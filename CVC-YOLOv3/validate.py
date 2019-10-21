#!/usr/bin/python3

import argparse
import random
import time
import os

from PIL import Image
import torch
import torchvision

from models import Darknet
from utils.datasets import ImageLabelDataset
from utils.nms import nms
from utils.utils import average_precision, bbox_iou, xywh2xyxy, calculate_padding, draw_labels_on_image, visualize_and_save_to_gcloud,xywh2xyxy,add_class_dimension_to_labels
from utils import storage_client
from tqdm import tqdm

################################################
from torchvision import transforms
import copy
################################################

################################################
gcloud_vis_path = "gs://mit-dut-driverless-internal/dumping-ground/visualization/"
gcloud_tmp_path = "/tmp/"
################################################

def main(*, batch_size, model_cfg, bbox_all, step, n_cpu):
    cuda = torch.cuda.is_available()
    device = torch.device('cuda:0' if cuda else 'cpu')
    random.seed(0)
    torch.manual_seed(0)
    if cuda:
        torch.cuda.manual_seed(0)
        torch.cuda.manual_seed_all(0)
        torch.backends.cudnn.benchmark = True
        torch.cuda.empty_cache()

    # Initiate model
    model = Darknet(model_cfg)
    validate_uri, _, weights_uri = model.get_links()
    _, _, _, _, bw = model.get_dataAug()
    num_images, _ = model.num_images()

    # Load weights
    weights_path = storage_client.get_file(weights_uri)
    model.load_weights(weights_path, model.get_start_weight_dim())
    model.to(device, non_blocking=True)

    # Get dataloader
    dataloader = torch.utils.data.DataLoader(
        ImageLabelDataset(validate_uri, height=img_height, width=img_width, augment_hsv=False,
                          augment_affine=False, num_images=num_images,
                          bw=bw, n_cpu=n_cpu, lr_flip=False, ud_flip=False),
        batch_size=batch_size,
        shuffle=False,
        num_workers=n_cpu,
        pin_memory=True)
    return validate(dataloader, model, device, step, bbox_all, tensorboard_writer=None)

# only works on a single class
def validate(*, dataloader, model, device, step=-1, bbox_all=False, tensorboard_writer=None,debug_mode,validation_mode):
        # result = open("logs/result.txt", "w" )

        with torch.no_grad():
            t_start = time.time()
            conf_thres, nms_thres, iou_thres = model.get_threshs()
            width, height = model.img_size()
            model.eval()
            print("Calculating mAP - Model in evaluation mode")
            n_images = len(dataloader.dataset)
            mAPs = []
            mR = []
            mP = []
            for batch_i, (img_uris, imgs, targets) in enumerate(tqdm(dataloader,desc='Computing mAP')):
                imgs = imgs.to(device, non_blocking=True)
                targets = targets.to(device, non_blocking=True)
                # output,_,_,_ = model(imgs)
                output = model(imgs)

                for sample_i, (labels, detections) in enumerate(zip(targets, output)):
                    detections = detections[detections[:, 4] > conf_thres]
                    if detections.size()[0] == 0:
                        predictions = torch.tensor([])
                    else:
                        predictions = torch.argmax(detections[:, 5:], dim=1)
                    # From (center x, center y, width, height) to (x1, y1, x2, y2)
                    box_corner = torch.zeros((detections.shape[0], 4), device=detections.device)
                    xy = detections[:, 0:2]
                    wh = detections[:, 2:4] / 2
                    box_corner[:, 0:2] = xy - wh
                    box_corner[:, 2:4] = xy + wh
                    probabilities = detections[:, 4]
                    nms_indices = nms(box_corner, probabilities, nms_thres)
                    box_corner = box_corner[nms_indices]
                    probabilities = probabilities[nms_indices]
                    predictions = predictions[nms_indices]

                    if nms_indices.shape[0] == 0:  # there should always be at least one label
                        continue
                    # Get detections sorted by decreasing confidence scores
                    _, inds = torch.sort(-probabilities)
                    box_corner = box_corner[inds]

                    probabilities = probabilities[inds]
                    predictions = predictions[inds]
                    labels = labels[(labels[:, 1:5] <= 0).sum(dim=1) == 0]  # remove the 0-padding added by the dataloader
                    # Extract target boxes as (x1, y1, x2, y2)
                    target_boxes = xywh2xyxy(labels[:, 1:5])
                    target_boxes[:, (0,2)] *= width
                    target_boxes[:, (1,3)] *= height
                    detected = torch.zeros(target_boxes.shape[0], device=target_boxes.device, dtype=torch.uint8)
                    correct = torch.zeros(nms_indices.shape[0], device=box_corner.device, dtype=torch.uint8)
                    # 0th dim is the detection
                    # (repeat in the 1st dim)
                    # 2nd dim is the coord
                    ious = bbox_iou(box_corner.unsqueeze(1).expand(-1, target_boxes.shape[0], -1),
                                    target_boxes.unsqueeze(0).expand(box_corner.shape[0], -1, -1))
                    # ious is 2d -- 0th dim is the detected box, 1st dim is the target box, value is iou

                    #######################################################
                    ##### skip images without label #####
                    if [] in ious.data.tolist():
                        continue
                    #######################################################

                    ##### validation between iou and bounding box #####
                    if validation_mode:
                        print("#################################################")

                        box_tensor_list = target_boxes.data.tolist()
                        box_size_list = []

                        for j in box_tensor_list:
                            box_width = abs(j[0]-j[2])
                            box_height = abs(j[1]-j[3])
                            box_size_list.append([box_width,box_height])

                        print(f"box_corner is {box_size_list}")
                        print(f"box_list length is {len(box_size_list)}")

                        iou_list = ious.data.tolist()
                        best_iou_list = []
                        for i in iou_list:
                            if max(i) == 0:
                                continue
                            best_iou_list.append(max(i))
                        
                        print(f"best iou list: {best_iou_list}")
                        print(f"iou list length is {len(best_iou_list)}")

                        validation_textfile = open('logs/iou_validation.txt', 'a')
                        if len(best_iou_list) <= len(box_size_list):
                            for i in range(len(best_iou_list)):
                                validation_textfile.write(f"{box_size_list[i]}:{best_iou_list[i]}\n")
                            validation_textfile.close()
                        else:
                            for i in range(len(box_size_list)):
                                validation_textfile.write(f"{box_size_list[i]}:{best_iou_list[i]}\n")
                            validation_textfile.close()
                    ###################################################

                    best_is = torch.argmax(ious, dim=1)

                    # TODO fix for multi-class. Need to use predictions somehow?
                    for i, iou in enumerate(ious):
                        best_i = best_is[i]
                        if ious[i, best_i] > iou_thres and detected[best_i] == 0:
                            correct[i] = 1
                            detected[best_i] = 1

                    # Compute Average Precision (AP) per class
                    ap, r, p = average_precision(tp=correct, conf=probabilities, n_gt=labels.shape[0])
                    if tensorboard_writer is not None:
                        pass
                        # TODO implement the pr curve; some parameter is incorrect.
                        # tensorboard_writer.add_pr_curve('validate_pr_curve',
                        #                                 labels=labels[:, 0],
                        #                                 predictions=probabilities,
                        #                                 global_step=step)

                    # Compute mean AP across all classes in this image, and append to image list
                    mAPs.append(ap)
                    mR.append(r)
                    mP.append(p)
                    if bbox_all or sample_i < 2:  # log the first two images in every batch
                        img_filepath = storage_client.get_uri_filepath(img_uris[sample_i])
                        if img_filepath is None:
                            print("NULL image filepath for image uri: {uri}".format(uri=img_uris[sample_i]))
                        orig_img = Image.open(img_filepath)
                        # draw = ImageDraw.Draw(img_with_boxes)
                        w, h = orig_img.size
                        pad_h, pad_w, scale_factor = calculate_padding(h, w, height, width)

                        ##################################
                        detect_box = copy.deepcopy(box_corner)
                        ##################################

                        box_corner /= scale_factor
                        box_corner[:, (0, 2)] -= pad_w
                        box_corner[:, (1, 3)] -= pad_h 

                        #######################################################################################
                        if debug_mode:
                            pil_img = transforms.ToPILImage()(imgs.squeeze())
                            ##### getting the image's name #####
                            img_path = storage_client.get_uri_filepath(img_uris[0])  
                            img_name = ("_".join(map(str, img_path.split("_")[-5:])))
                            gcloud_save_path = gcloud_vis_path + img_name[:-4] + "_predicted_vis.jpg"
                            tmp_path = os.path.join(gcloud_tmp_path, img_name[:-4] + "_predicted_vis.jpg")
                            vis_label = add_class_dimension_to_labels(detect_box)
                            visualize_and_save_to_gcloud(pil_img, vis_label, gcloud_save_path, tmp_path,box_color="red")
                            print("Prediction visualization uploaded")
                        #######################################################################################

                        if tensorboard_writer is not None:
                            if step == 1:  # if the first step
                                ground_truth_box_corner = labels[:, 1:5]
                                ground_truth_box_corner[:, (0, 2)] *= width
                                ground_truth_box_corner[:, (1, 3)] *= height
                                ground_truth_box_corner /= scale_factor
                                ground_truth_box_corner[:, 0] -= pad_w
                                ground_truth_box_corner[:, 1] -= pad_h
                                ground_truth_box_corner = xywh2xyxy(ground_truth_box_corner)
                                ground_truth_img = draw_labels_on_image(orig_img,
                                                                        labels=labels[:, 0].int(),
                                                                        boxes=ground_truth_box_corner.int().tolist(),
                                                                        label_names=["cone", "unused", "unused"],
                                                                        label_colors=["blue", "black", "red"])
                                tensorboard_writer.add_image(f'img/{img_uris[sample_i]}/ground_truth',
                                                            torchvision.transforms.functional.to_tensor(ground_truth_img),
                                                            global_step=step)
                            predicted_image = draw_labels_on_image(orig_img,
                                                                    labels=predictions,
                                                                    boxes=box_corner.int().tolist(),
                                                                    label_names=["cone", "unused", "unused"],
                                                                    label_colors=["blue", "black", "red"])
                            tensorboard_writer.add_image(f'img/{img_uris[sample_i]}/predicted',
                                                        torchvision.transforms.functional.to_tensor(predicted_image),
                                                        global_step=step)

                mean_mAP = torch.tensor(mAPs, dtype=torch.float).mean().item()
                mean_R = torch.tensor(mR, dtype=torch.float).mean().item()
                mean_P = torch.tensor(mP, dtype=torch.float).mean().item()
            # Means of all images
            mean_mAP = torch.tensor(mAPs, dtype=torch.float).mean().item()
            mean_R = torch.tensor(mR, dtype=torch.float).mean().item()
            mean_P = torch.tensor(mP, dtype=torch.float).mean().item()
            dt = time.time() - t_start
            print('mAP: {0:5.2%}, Recall: {1:5.2%}, Precision: {2:5.2%}'.format(mean_mAP, mean_R, mean_P))
            # result.write(str(1-mean_mAP))
            # result.close() 
            return mean_mAP, mean_R, mean_P, dt/(n_images + 1e-12)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    def add_bool_arg(name, default, help):
        group = parser.add_mutually_exclusive_group(required=False)
        group.add_argument('--' + name, dest=name, action='store_true', help=help)
        group.add_argument('--no_' + name, dest=name, action='store_false', help=("Do not " + help))
        parser.set_defaults(**{name:default})
    
    parser.add_argument('--batch_size', type=int, help='size of each image batch')
    parser.add_argument('--model_cfg', type=str, help='path to model config file')
    add_bool_arg('bbox_all', default=False, help="whether to draw bounding boxes on all images")
    parser.add_argument('--step', type=int, default=-1, help='the step at which these images were generated')
    parser.add_argument('--n_cpu', type=int, default=0, help='number of cpu threads to use during batch generation')

    opt = parser.parse_args()
    results = main(batch_size=opt.batch_size, model_cfg=opt.model_cfg, bbox_all=opt.bbox_all, step=opt.step, n_cpu=opt.n_cpu)