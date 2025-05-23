# loading the finetuned weights to evulate the performance

import os
import argparse
import json
import copy
import torch
import torch.backends.cudnn as cudnn
import utils
import models
import numpy as np
from models.head import ClsHead

from pathlib import Path
from torch import nn
from torchvision import transforms as pth_transforms
from torch.utils.data import Dataset
from PIL import Image

import torch.nn.functional as F
from sklearn.metrics import (
    accuracy_score, roc_auc_score, f1_score, average_precision_score,
    hamming_loss, jaccard_score, recall_score, precision_score, cohen_kappa_score
)
from collections import defaultdict


def pil_loader(path):
    with open(path, 'rb') as f:
        img = Image.open(f)
        return img.convert('RGB')


class RETFoundDataset(Dataset):
    def __init__(self, root, split, transform=None):
        self.data = []
        if 'PAPILA' in root:
            dr_folder_list = ['anormal', 'bsuspectglaucoma', 'cglaucoma']
        elif 'Glaucoma_fundus' in root:
            dr_folder_list = ['anormal_control', 'bearly_glaucoma', 'cadvanced_glaucoma']
        elif 'Retina' in root:
            dr_folder_list = ['anormal', 'bcataract', 'cglaucoma', 'ddretina_disease']
        elif 'OCTID' in root:
            dr_folder_list = ['ANormal', 'ARMD', 'CSR', 'Diabetic_retinopathy', 'Macular_Hole']
        elif 'JSIEC' in root:
            dr_folder_list = ['0.0.Normal', '20.Massive hard exudates',
                              '0.1.Tessellated fundus', '21.Yellow-white spots-flecks',
                              '0.2.Large optic cup', '22.Cotton-wool spots',
                              '0.3.DR1', '23.Vessel tortuosity',
                              '1.0.DR2', '24.Chorioretinal atrophy-coloboma',
                              '1.1.DR3', '25.Preretinal hemorrhage',
                              '10.0.Possible glaucoma', '26.Fibrosis',
                              '10.1.Optic atrophy', '27.Laser Spots',
                              '11.Severe hypertensive retinopathy', '28.Silicon oil in eye',
                              '12.Disc swelling and elevation', '29.0.Blur fundus without PDR',
                              '13.Dragged Disc', '29.1.Blur fundus with suspected PDR',
                              '14.Congenital disc abnormality', '3.RAO',
                              '15.0.Retinitis pigmentosa', '4.Rhegmatogenous RD',
                              '15.1.Bietti crystalline dystrophy', '5.0.CSCR',
                              '16.Peripheral retinal degeneration and break', '5.1.VKH disease',
                              '17.Myelinated nerve fiber', '6.Maculopathy',
                              '18.Vitreous particles', '7.ERM',
                              '19.Fundus neoplasm', '8.MH',
                              '2.0.BRVO', '9.Pathological myopia',
                              '2.1.CRVO']
        elif 'IDRiD' in root:
            dr_folder_list = ['anoDR', 'bmildDR', 'cmoderateDR', 'dsevereDR', 'eproDR']
        else:
            dr_folder_list = ['anodr', 'bmilddr', 'cmoderatedr', 'dseveredr', 'eproliferativedr']

        for lbl, lbl_name in enumerate(dr_folder_list):
            img_files = os.listdir(os.path.join(root, split, lbl_name))
            for img_f in img_files:
                img_fpath = os.path.join(root, split, lbl_name, img_f)
                self.data.append({'img_fpath': img_fpath, 'label': lbl})
        self.transform = transform

    def __getitem__(self, index):
        entry = self.data[index]
        img = pil_loader(entry['img_fpath'])
        if self.transform is not None:
            img = self.transform(img)
        return img, entry['label']

    def __len__(self):
        return len(self.data)


def convert_to_one_hot(gts):
    gts_one_hot = np.zeros((gts.shape[0], len(np.unique(gts))))
    for i in range(len(gts)):
        gts_one_hot[i][gts[i][0]] = 1

    return gts_one_hot


def eval_linear(args):
    utils.init_distributed_mode(args)
    cudnn.benchmark = True

    # fix the seed for reproducibility
    utils.fix_random_seeds(args.seed)

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    # ============ preparing data ... ============
    pth_transforms.ToTensor(),

    mean, std = utils.get_stats(args.modality)
    print(f"use the {args.modality} mean and std: {mean} and {std}")

    train_transform = pth_transforms.Compose([
        pth_transforms.RandomResizedCrop(args.input_size),
        pth_transforms.RandomHorizontalFlip(),
        pth_transforms.RandomVerticalFlip(),
        pth_transforms.ToTensor(),
        pth_transforms.Normalize(mean, std),
    ])

    val_transform = pth_transforms.Compose([
        pth_transforms.Resize(size=(args.input_size, args.input_size), interpolation=3),
        pth_transforms.ToTensor(),
        pth_transforms.Normalize(mean, std),
    ])

    print(f"-------- Current Task: {args.task} Modality: {args.modality} -------")

    dataset_val = RETFoundDataset(root=args.data_path, split='test', transform=val_transform)

    # sampler = torch.utils.data.distributed.DistributedSampler(dataset_train)
    val_loader = torch.utils.data.DataLoader(
        dataset_val,
        batch_size=args.batch_size_per_gpu,
        num_workers=args.num_workers,
        pin_memory=True,
        shuffle=False
    )
    print(f"Data loaded with {len(dataset_val)} test imgs.")

    # ============ building network ... ============
    model = models.__dict__[args.arch](
        img_size=[args.input_size],
        patch_size=args.patch_size,
        num_classes=0,
        use_mean_pooling=args.avgpool_patchtokens == 1)
    embed_dim = model.embed_dim
    model.cuda()
    print(f"Model {args.arch} {args.patch_size}x{args.patch_size} built.")
    # load visionfm pretrained weights
    utils.load_pretrained_weights(model, args.pretrained_weights, args.checkpoint_key, args.arch, args.patch_size)

    linear_classifier = ClsHead(embed_dim=embed_dim * 4, num_classes=args.num_labels, layers=3)
    linear_classifier = linear_classifier.cuda()
    linear_classifier = nn.parallel.DistributedDataParallel(linear_classifier, device_ids=[args.gpu])

    # load the weights
    state_dict = torch.load(args.pretrained_weights, map_location="cpu")
    state_dict = state_dict.get('classifier_state_dict',None)
    if state_dict is None:
        print("Cannot find the weights for classifier (decoder). Please refer to our fine-tuning instruction!!")
        print("The classifier would utlize the random weights!!")
    else:
        msg = linear_classifier.load_state_dict(state_dict, strict=False)
        print('Pretrained weights found at {} and loaded with msg: {}'.format(args.pretrained_weights, msg))

    model.eval()
    linear_classifier.eval()
    test_stats, output, target = validate_network(val_loader, model, linear_classifier, args.n_last_blocks,
                                                  args.avgpool_patchtokens)

    output = np.vstack(output)
    target = np.vstack(target)

    # auroc = roc_auc_score(target, output, average='macro', multi_class='ovr')
    # test_stats['auc'] = auroc


    # target_one_hot = convert_to_one_hot(target)
    # aupr = average_precision_score(target_one_hot, output, average='macro')
    # test_stats['aupr'] = aupr

    # print(f"AUC: {auroc}, AUPR: {aupr}")

    np.save(os.path.join(args.output_dir, 'best.npy'), output)
    np.save(os.path.join(args.output_dir, 'target.npy'), target)



@torch.no_grad()
def validate_network(val_loader, model, linear_classifier, n, avgpool):
    model.eval()
    linear_classifier.eval()
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'
    # targets, preds = [], []
    true_onehot, pred_onehot, true_labels, pred_labels, pred_softmax = [], [], [], [], []

    for inp, target in metric_logger.log_every(val_loader, 20, header):
        # move to gpu
        inp = inp.cuda(non_blocking=True)
        target = target.cuda(non_blocking=True)

        # forward
        with torch.no_grad():
            intermediate_output = model.get_intermediate_layers(inp, n)
            if avgpool == 0:
                output = [x[:, 0] for x in intermediate_output]
            elif avgpool == 1:
                output = [torch.mean(intermediate_output[-1][:, 1:], dim=1)]
            elif avgpool == 2:
                output = [x[:, 0] for x in intermediate_output] + [torch.mean(intermediate_output[-1][:, 1:], dim=1)]
            else:
                assert False, "Unkown avgpool type {}".format(avgpool)

            output = torch.cat(output, dim=-1)

        output = linear_classifier(output)

        num_class = output.shape[1]
        if num_class > 1:  # multi-class case
            loss = nn.CrossEntropyLoss()(output, target)

            output_softmax = nn.Softmax(dim=1)(output)
            output_label = output_softmax.argmax(dim=1)
            target_onehot = F.one_hot(target.to(torch.int64), num_classes=num_class)
            output_onehot = F.one_hot(output_label.to(torch.int64), num_classes=num_class)
        else:
            loss = nn.BCEWithLogitsLoss()(output.squeeze(dim=1), target.float())

            output_softmax = output.sigmoid()
            output_label = (output_softmax > 0.5).float()
            target_onehot = target.unsqueeze(1)
            output_onehot = output_label.unsqueeze(1)

        # save results
        # if num_class > 1:  # multi-classes
        #     preds.append(output.softmax(dim=1).detach().cpu().numpy())
        #     targets.append(np.expand_dims(target.detach().cpu().numpy(), axis=1))
        # else:  # binary classification
        #     preds.append(output.detach().cpu().sigmoid().numpy())
        #     targets.append(np.expand_dims(target.detach().cpu().numpy(), axis=1))

        metric_logger.update(loss=loss.item())

        # metrics
        true_onehot.extend(target_onehot.cpu().numpy())
        pred_onehot.extend(output_onehot.detach().cpu().numpy())
        true_labels.extend(target.cpu().numpy())
        pred_labels.extend(output_label.detach().cpu().numpy())
        pred_softmax.extend(output_softmax.detach().cpu().numpy())

    print('* test loss {losses.global_avg:.4f} '.format(losses=metric_logger.loss))

    metrics = {k: meter.global_avg for k, meter in metric_logger.meters.items()}

    accuracy = accuracy_score(true_labels, pred_labels)
    hamming = hamming_loss(true_onehot, pred_onehot)
    jaccard = jaccard_score(true_onehot, pred_onehot, average='macro')
    average_precision = average_precision_score(true_onehot, pred_softmax, average='macro')
    kappa = cohen_kappa_score(true_labels, pred_labels)
    f1 = f1_score(true_onehot, pred_onehot, zero_division=0, average='macro')
    roc_auc = roc_auc_score(true_onehot, pred_softmax, multi_class='ovr', average='macro')
    precision = precision_score(true_onehot, pred_onehot, zero_division=0, average='macro')
    recall = recall_score(true_onehot, pred_onehot, zero_division=0, average='macro')
    
    score = (f1 + roc_auc + kappa) / 3

    metrics.update({
        'accuracy': accuracy,
        'f1': f1,
        'roc_auc': roc_auc,
        'hamming': hamming,
        'jaccard': jaccard,
        'precision': precision,
        'recall': recall,
        'average_precision': average_precision,
        'kappa': kappa,
        'score': score,
        'aupr': average_precision,  # Same as average_precision but named to match reference
        'auc': roc_auc  # Same as roc_auc but named to match reference
    })

    print(f'* test loss {metric_logger.loss.global_avg:.4f}')
    print(f'Accuracy: {accuracy:.4f}, F1 Score: {f1:.4f}, ROC AUC: {roc_auc:.4f}, Hamming Loss: {hamming:.4f},\n'
          f' Jaccard Score: {jaccard:.4f}, Precision: {precision:.4f}, Recall: {recall:.4f},\n'
          f' Average Precision: {average_precision:.4f}, Kappa: {kappa:.4f}, Score: {score:.4f}')

    return metrics, pred_softmax, true_labels
    # return {k: meter.global_avg for k, meter in metric_logger.meters.items()}, preds, targets

if __name__ == '__main__':
    parser = argparse.ArgumentParser('Evaluating VisionFM for multi-class classification using a test set')
    parser.add_argument('--n_last_blocks', default=4, type=int)
    parser.add_argument('--avgpool_patchtokens', default=0, choices=[0, 1, 2], type=int,
        help="""Whether or not to use global average pooled features or the [CLS] token.""")
    parser.add_argument('--arch', default='vit_base', type=str, choices=['vit_tiny', 'vit_small', 'vit_base',
        'vit_large', 'swin_tiny','swin_small', 'swin_base', 'swin_large', 'resnet50', 'resnet101', 'dalle_encoder'], help='Architecture.')
    parser.add_argument('--input_size', type=int, default=224, help='Input size')
    parser.add_argument('--patch_size', default=16, type=int, help='Patch resolution of the model.')
    parser.add_argument('--window_size', default=7, type=int, help='Window size of the model.')
    parser.add_argument('--pretrained_weights', default='', type=str, help="""Path to pretrained 
        weights""")
    parser.add_argument("--checkpoint_key", default="visionfm_state_dict", type=str, help='Key to use in the checkpoint (example: "teacher")')
    parser.add_argument('--epochs', default=100, type=int, help='Number of epochs of finetuning.')
    parser.add_argument("--lr", default=0.001, type=float, help="""Learning rate at the beginning of
        training the classifier""")
    parser.add_argument('--batch_size_per_gpu', default=128, type=int, help='Per-GPU batch-size')
    parser.add_argument("--dist_url", default="env://", type=str, help="""url used to set up
        distributed training; see https://pytorch.org/docs/stable/distributed.html""")
    parser.add_argument("--local_rank", default=0, type=int, help="Please ignore and do not set this argument.")
    parser.add_argument('--data_path', default='/path/to/dataset/', type=str,
        help='Please specify path to the eye image data.')
    parser.add_argument('--seed', default=0, type=int)
    parser.add_argument('--modality', default='Fundus', type=str)
    parser.add_argument('--task', default='PAPILA', type=str)
    parser.add_argument('--extra', default='', type=str)
    parser.add_argument('--num_workers', default=10, type=int, help='Number of data loading workers per GPU.')
    parser.add_argument('--val_freq', default=1, type=int, help="Epoch frequency for validation.")
    parser.add_argument('--output_dir', default=".", help='Path to save logs and checkpoints')
    parser.add_argument('--num_labels', default=1000, type=int, help='Number of labels for linear classifier')
    parser.add_argument('--load_from', default=None, help='Path to load checkpoints to resume finetuning')
    args = parser.parse_args()

    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    for checkpoint_key in args.checkpoint_key.split(','):
        print("Start finetuning {}.".format(checkpoint_key))
        args_copy = copy.deepcopy(args)
        args_copy.checkpoint_key = checkpoint_key
        eval_linear(args_copy)
