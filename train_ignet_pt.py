""" Training routine for GraspNet baseline model. """

import os
import sys
import numpy as np
from datetime import datetime
import argparse

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from torch.optim.lr_scheduler import ExponentialLR, MultiStepLR, CosineAnnealingLR, OneCycleLR

import resource
# RuntimeError: received 0 items of ancdata. Issue: pytorch/pytorch#973
rlimit = resource.getrlimit(resource.RLIMIT_NOFILE)
hard_limit = rlimit[1]
soft_limit = min(500000, hard_limit)
print("soft limit: ", soft_limit, "hard limit: ", hard_limit)
resource.setrlimit(resource.RLIMIT_NOFILE, (soft_limit, hard_limit))

import cv2
cv2.setNumThreads(0)
cv2.ocl.setUseOpenCL(False)

import random
def setup_seed(seed):
     torch.manual_seed(seed)
     torch.cuda.manual_seed_all(seed)
     np.random.seed(seed)
     random.seed(seed)
     torch.backends.cudnn.deterministic = True
# 设置随机数种子
setup_seed(0)

from models.IGNet_loss import get_loss
from dataset.ignet_dataset import GraspNetDataset, pt_collate_fn, load_grasp_labels
from models.point_transformer.model import Ignet_pt

parser = argparse.ArgumentParser()
parser.add_argument('--dataset_root', default='/media/gpuadmin/rcao/dataset/graspnet', help='Dataset root')
parser.add_argument('--camera', default='realsense', help='Camera split [realsense/kinect]')
parser.add_argument('--checkpoint_path', default=None, help='Model checkpoint path [default: None]')
parser.add_argument('--log_dir', default='log/ignet_v0.5', help='Dump dir to save model checkpoint [default: log]')
parser.add_argument('--num_point', type=int, default=512, help='Point Number [default: 1024]')
# parser.add_argument('--seed_feat_dim', default=256, type=int, help='Point wise feature dim')
parser.add_argument('--voxel_size', type=int, default=0.002, help='Voxel Size for Quantize [default: 0.005]')
parser.add_argument('--visib_threshold', type=int, default=0.5, help='Visibility Threshold [default: 0.5]')
parser.add_argument('--num_view', type=int, default=300, help='View Number [default: 300]')
parser.add_argument('--max_epoch', type=int, default=61, help='Epoch to run [default: 18]')
parser.add_argument('--batch_size', type=int, default=30, help='Batch Size during training [default: 2]')
parser.add_argument('--learning_rate', type=float, default=0.001, help='Initial learning rate [default: 0.001]')
parser.add_argument('--worker_num', type=int, default=16, help='Worker number for dataloader [default: 4]')
cfgs = parser.parse_args()

# ------------------------------------------------------------------------- GLOBAL CONFIG BEG
EPOCH_CNT = 0
# LR_DECAY_STEPS = [int(x) for x in cfgs.lr_decay_steps.split(',')]
# LR_DECAY_RATES = [float(x) for x in cfgs.lr_decay_rates.split(',')]
# assert(len(LR_DECAY_STEPS)==len(LR_DECAY_RATES))
DEFAULT_CHECKPOINT_PATH = os.path.join(cfgs.log_dir, 'checkpoint.tar')
CHECKPOINT_PATH = cfgs.checkpoint_path if cfgs.checkpoint_path is not None \
    else DEFAULT_CHECKPOINT_PATH

if not os.path.exists(cfgs.log_dir):
    os.makedirs(cfgs.log_dir)

LOG_FOUT = open(os.path.join(cfgs.log_dir, 'log_train.txt'), 'a')
LOG_FOUT.write(str(cfgs)+'\n')
def log_string(out_str):
    LOG_FOUT.write(out_str+'\n')
    LOG_FOUT.flush()
    print(out_str)

# Init datasets and dataloaders 
def my_worker_init_fn(worker_id):
    np.random.seed(np.random.get_state()[1][0] + worker_id)
    pass

device = torch.device("cuda:3" if torch.cuda.is_available() else "cpu")
torch.cuda.set_device(device)

# Create Dataset and Dataloader
valid_obj_idxs, grasp_labels = load_grasp_labels(cfgs.dataset_root)
TRAIN_DATASET = GraspNetDataset(cfgs.dataset_root, valid_obj_idxs, grasp_labels, camera=cfgs.camera, split='train', 
                                num_points=cfgs.num_point, remove_outlier=True, augment=True, real_data=True, 
                                syn_data=True, visib_threshold=cfgs.visib_threshold, voxel_size=cfgs.voxel_size)
TEST_DATASET = GraspNetDataset(cfgs.dataset_root, valid_obj_idxs, grasp_labels, camera=cfgs.camera, split='test_seen', 
                               num_points=cfgs.num_point, remove_outlier=True, augment=False, real_data=True, 
                               syn_data=False, visib_threshold=cfgs.visib_threshold, voxel_size=cfgs.voxel_size)

print(len(TRAIN_DATASET), len(TEST_DATASET))
# TRAIN_DATALOADER = DataLoader(TRAIN_DATASET, batch_size=cfgs.batch_size, shuffle=True,
#     num_workers=4, worker_init_fn=my_worker_init_fn, collate_fn=collate_fn)
# TEST_DATALOADER = DataLoader(TEST_DATASET, batch_size=cfgs.batch_size, shuffle=False,
#     num_workers=4, worker_init_fn=my_worker_init_fn, collate_fn=collate_fn)
TRAIN_DATALOADER = DataLoader(TRAIN_DATASET, batch_size=cfgs.batch_size, shuffle=True,
    num_workers=cfgs.worker_num, worker_init_fn=my_worker_init_fn, collate_fn=pt_collate_fn)
TEST_DATALOADER = DataLoader(TEST_DATASET, batch_size=cfgs.batch_size, shuffle=False,
    num_workers=cfgs.worker_num, worker_init_fn=my_worker_init_fn, collate_fn=pt_collate_fn)
print(len(TRAIN_DATALOADER), len(TEST_DATALOADER))

# Init the model and optimzier
net = Ignet_pt(num_view=cfgs.num_view, voxel_size=cfgs.voxel_size, is_training=True)
net.to(device)

# Load the Adam optimizer
# optimizer = optim.Adam(net.parameters(), lr=cfgs.learning_rate, weight_decay=cfgs.weight_decay)
optimizer = optim.AdamW(net.parameters(), lr=cfgs.learning_rate)
lr_scheduler = CosineAnnealingLR(optimizer, T_max=16, eta_min=1e-5)

# Load checkpoint if there is any
it = -1 # for the initialize value of `LambdaLR` and `BNMomentumScheduler`
start_epoch = 0
if CHECKPOINT_PATH is not None and os.path.isfile(CHECKPOINT_PATH):
    checkpoint = torch.load(CHECKPOINT_PATH)
    net.load_state_dict(checkpoint['model_state_dict'])
    optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
    start_epoch = checkpoint['epoch']
    log_string("-> loaded checkpoint %s (epoch: %d)"%(CHECKPOINT_PATH, start_epoch))

# Decay Batchnorm momentum from 0.5 to 0.999
# note: pytorch's BN momentum (default 0.1)= 1 - tensorflow's BN momentum
# BN_MOMENTUM_INIT = 0.5
# BN_MOMENTUM_MAX = 0.001
# bn_lbmd = lambda it: max(BN_MOMENTUM_INIT * cfgs.bn_decay_rate**(int(it / cfgs.bn_decay_step)), BN_MOMENTUM_MAX)
# bnm_scheduler = BNMomentumScheduler(net, bn_lambda=bn_lbmd, last_epoch=start_epoch-1)
# scheduler = OneCycleLR(optimizer, max_lr=cfgs.learning_rate, steps_per_epoch=len(TRAIN_DATALOADER),
                    #    epochs=cfgs.max_epoch, last_epoch=start_epoch * len(TRAIN_DATALOADER)-1)

# TensorBoard Visualizers
TRAIN_WRITER = SummaryWriter(os.path.join(cfgs.log_dir, 'train'))
TEST_WRITER = SummaryWriter(os.path.join(cfgs.log_dir, 'test'))

# ------------------------------------------------------------------------- GLOBAL CONFIG END
def train_one_epoch():
    stat_dict = {} # collect statistics
    # adjust_learning_rate(optimizer, EPOCH_CNT)
    # bnm_scheduler.step() # decay BN momentum
    # set model to training mode
    net.train()
    overall_loss = 0
    for batch_idx, batch_data_label in enumerate(TRAIN_DATALOADER):
        for key in batch_data_label:
            if 'list' in key:
                for i in range(len(batch_data_label[key])):
                    for j in range(len(batch_data_label[key][i])):
                        batch_data_label[key][i][j] = batch_data_label[key][i][j].cuda()
            else:
                batch_data_label[key] = batch_data_label[key].cuda()
        # Forward pass
        end_points = net(batch_data_label)

        # Compute loss and gradients, update parameters.
        loss, end_points = get_loss(end_points, device)
        loss.backward()
        # if (batch_idx+1) % 1 == 0:
        optimizer.step()
        optimizer.zero_grad()
        # scheduler.step()
        
        # Accumulate statistics and print out
        for key in end_points:
            if 'loss' in key or 'acc' in key or 'prec' in key or 'recall' in key or 'count' in key:
                if key not in stat_dict: stat_dict[key] = 0
                stat_dict[key] += end_points[key].item()

        overall_loss += stat_dict['loss/overall_loss']
        batch_interval = 10
        if (batch_idx+1) % batch_interval == 0:
            log_string(' ---- batch: %03d ----' % (batch_idx+1))
            for key in sorted(stat_dict.keys()):
                TRAIN_WRITER.add_scalar(key, stat_dict[key]/batch_interval, (EPOCH_CNT*len(TRAIN_DATALOADER)+batch_idx)*cfgs.batch_size)
                log_string('mean %s: %f'%(key, stat_dict[key]/batch_interval))
                stat_dict[key] = 0
    
    log_string('overall loss:{}, batch num:{}'.format(overall_loss, batch_idx+1))
    mean_loss = overall_loss/float(batch_idx+1)
    return mean_loss


def evaluate_one_epoch():
    stat_dict = {} # collect statistics
    # set model to eval mode (for bn and dp)
    net.eval()
    overall_loss = 0
    for batch_idx, batch_data_label in enumerate(TEST_DATALOADER):
        if batch_idx % 10 == 0:
            log_string('Eval batch: %d'%(batch_idx))
        for key in batch_data_label:
            if 'list' in key:
                for i in range(len(batch_data_label[key])):
                    for j in range(len(batch_data_label[key][i])):
                        batch_data_label[key][i][j] = batch_data_label[key][i][j].cuda()
            else:
                batch_data_label[key] = batch_data_label[key].cuda()
        # Forward pass
        with torch.no_grad():
            end_points = net(batch_data_label)

        # Compute loss
        loss, end_points = get_loss(end_points, device)

        # Accumulate statistics and print out
        for key in end_points:
            if 'loss' in key or 'acc' in key or 'prec' in key or 'recall' in key or 'count' in key:
                if key not in stat_dict: stat_dict[key] = 0
                stat_dict[key] += end_points[key].item()
    
        overall_loss += stat_dict['loss/overall_loss']
        
    for key in sorted(stat_dict.keys()):
        TEST_WRITER.add_scalar(key, stat_dict[key]/float(batch_idx+1), (EPOCH_CNT+1)*len(TRAIN_DATALOADER)*cfgs.batch_size)
        log_string('eval mean %s: %f'%(key, stat_dict[key]/(float(batch_idx+1))))

    log_string('overall loss:{}, batch num:{}'.format(overall_loss, batch_idx+1))
    mean_loss = overall_loss/float(batch_idx+1)
    return mean_loss


def train(start_epoch):
    global EPOCH_CNT 
    min_loss = np.inf
    loss = 0
    best_epoch = 0
    for epoch in range(start_epoch, cfgs.max_epoch):
        EPOCH_CNT = epoch
        log_string('**** EPOCH %03d ****' % (epoch))
        log_string('Current learning rate: %f' % (lr_scheduler.get_last_lr()[0]))
        # log_string('Current BN decay momentum: %f'%(bnm_scheduler.lmbd(bnm_scheduler.last_epoch)))
        log_string(str(datetime.now()))
        # Reset numpy seed.
        # REF: https://github.com/pytorch/pytorch/issues/5059
        np.random.seed()
        train_loss = train_one_epoch()
        lr_scheduler.step()
        
        eval_loss = evaluate_one_epoch()
        # Save checkpoint
        save_dict = {'epoch': epoch+1, # after training one epoch, the start_epoch should be epoch+1
                    'optimizer_state_dict': optimizer.state_dict(),
                    # 'loss': loss,
                    'lr_scheduler':lr_scheduler.state_dict(),
                    }
        try: # with nn.DataParallel() the net is added as a submodule of DataParallel
            save_dict['model_state_dict'] = net.module.state_dict()
        except:
            save_dict['model_state_dict'] = net.state_dict()
        
        if eval_loss < min_loss:
            min_loss = eval_loss
            best_epoch = epoch
            ckpt_name = "epoch_" + str(best_epoch) \
                        + "_train_" + str(train_loss) \
                        + "_val_" + str(eval_loss)
            torch.save(save_dict, os.path.join(cfgs.log_dir, ckpt_name + '.tar'))
        log_string("best_epoch:{}".format(best_epoch))
        # if epoch in LR_DECAY_STEPS:
        #     torch.save(save_dict, os.path.join(cfgs.log_dir, 'checkpoint_{}.tar'.format(epoch)))
        # if not EPOCH_CNT % 5:
        #     torch.save(save_dict, os.path.join(cfgs.log_dir, 'checkpoint_{}.tar'.format(EPOCH_CNT)))


if __name__=='__main__':
    train(start_epoch)
