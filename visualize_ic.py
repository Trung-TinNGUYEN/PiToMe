import argparse
import datetime
import numpy as np
import time
import torch
import torch.backends.cudnn as cudnn
import json
import os
import pandas as pd

from pathlib import Path

from timm.data import Mixup
from timm.models import create_model
from timm.loss import LabelSmoothingCrossEntropy, SoftTargetCrossEntropy
from timm.scheduler import create_scheduler
from timm.optim import create_optimizer
from timm.utils import NativeScaler, get_state_dict, ModelEma

from datasets import load_dataset
from ic.engine import train_one_epoch, evaluate
from ic.samplers import RASampler
import ic.utils as utils
from ic.utils import MultiEpochsDataLoader
from timm.scheduler.cosine_lr import CosineLRScheduler

from datasets import load_dataset
from torchvision import transforms
import torch
import os
from main_ic import process_image
import algo.tome as tome
import algo.pitome as pitome
import algo.DiffRate as DiffRate
import ic.models_mae as models_mae 
from main_ic import get_args_parser
from ic.utils import build_transform, DATA_PATH

def get_tome_model(model, args):
    if 'deit' in model_ckt:
        tome.patch.deit(model,use_k=args.use_k)
        model.ratio=float(args.ratio)
        # model.r=int(args.r)
    elif 'mae' in args.model:
        tome.patch.mae(model,use_k=args.use_k)
        model.ratio=float(args.ratio)
        # model.r=int(args.r)
    else:
        raise ValueError("only support deit, mae and caformer in this codebase")
    

def get_pitome_model(model, args):
    if 'deit' in args.model:
        pitome.patch.deit(model,use_k=args.use_k)
        model.ratio=float(args.ratio)
        # model.r=int(args.r)
    elif 'mae' in args.model:
        pitome.patch.mae(model,use_k=args.use_k)
        model.ratio=float(args.ratio)
        # model.r=int(args.r)
    else:
        raise ValueError("only support deit, mae and caformer in this codebase")



def get_diffrate_model(model, args):
    if 'deit' in args.model:
        DiffRate.patch.deit(model, prune_granularity=args.granularity, merge_granularity=args.granularity)
    elif 'mae' in args.model:
        DiffRate.patch.mae(model, prune_granularity=args.granularity, merge_granularity=args.granularity)
    else:
        raise ValueError("only support deit, mae and caformer in this codebase")

    if args.use_k:
        model.init_kept_num_using_r(args.ratio)
    else:
        model.init_kept_num_using_ratio(args.ratio)
    
            

def main(args, model ,logger):
    # utils.setup_default_logging()

 
            
    if args.finetune:
        if args.finetune.startswith('https'):
            checkpoint = torch.hub.load_state_dict_from_url(
                args.finetune, map_location='cuda', check_hash=True)
        else:
            checkpoint = torch.load(args.finetune, map_location='cuda')

        checkpoint_model = checkpoint['model']
        state_dict = model.state_dict()
        for k in ['head.weight', 'head.bias', 'head_dist.weight', 'head_dist.bias']:
            if k in checkpoint_model and checkpoint_model[k].shape != state_dict[k].shape:
                logger.info(f"Removing key {k} from pretrained checkpoint")
                del checkpoint_model[k]

        # interpolate position embedding
        pos_embed_checkpoint = checkpoint_model['pos_embed']
        embedding_size = pos_embed_checkpoint.shape[-1]
        num_patches = model.patch_embed.num_patches
        num_extra_tokens = model.pos_embed.shape[-2] - num_patches
        # height (== width) for the checkpoint position embedding
        orig_size = int((pos_embed_checkpoint.shape[-2] - num_extra_tokens) ** 0.5)
        # height (== width) for the new position embedding
        new_size = int(num_patches ** 0.5)
        # class_token and dist_token are kept unchanged
        extra_tokens = pos_embed_checkpoint[:, :num_extra_tokens]
        # only the position tokens are interpolated
        pos_tokens = pos_embed_checkpoint[:, num_extra_tokens:]
        pos_tokens = pos_tokens.reshape(-1, orig_size, orig_size, embedding_size).permute(0, 3, 1, 2)
        pos_tokens = torch.nn.functional.interpolate(
            pos_tokens, size=(new_size, new_size), mode='bicubic', align_corners=False)
        pos_tokens = pos_tokens.permute(0, 2, 3, 1).flatten(1, 2)
        new_pos_embed = torch.cat((extra_tokens, pos_tokens), dim=1)
        checkpoint_model['pos_embed'] = new_pos_embed
        model.load_state_dict(checkpoint_model, strict=False)


    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    logger.info(f'number of params: {n_parameters}')

    linear_scaled_lr = args.lr * args.batch_size * utils.get_world_size() / 512.0
    args.lr = linear_scaled_lr


    test_stats = evaluate(data_loader_val, model, device,logger)
    logger.info(f"Accuracy of the network on the {len(dataset_val)} test images: {test_stats['acc1']:.1f}%")
    return test_stats


model_name_dict = {
    'deit_tiny_patch16_224':'ViT-T-DeiT',
    'deit_small_patch16_224':'ViT-S-DeiT',
    'deit_base_patch16_224': 'ViT-B-DeiT',
    'vit_base_patch16_mae': 'ViT-B-MAE',
    'vit_large_patch16_mae': 'ViT-L-MAE',
    'vit_huge_patch14_mae': 'ViT-H-MAE',
}

if __name__ == '__main__':
    parser = argparse.ArgumentParser('evaluation script', parents=[get_args_parser()])
    args = parser.parse_args()
    utils.init_distributed_mode(args)

    output_dir = Path(args.output_dir)
    logger = utils.create_logger(output_dir,dist_rank=utils.get_rank())
    wandb = utils.Wandb()
    logger.info(args)
    args.device= 'cuda:2'

    device = torch.device(args.device)

    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)

    cudnn.benchmark = True
    dataset = load_dataset("imagenet-1k", cache_dir=f"{DATA_PATH}/imagenet/")
    transform = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.ToTensor(),
    ])
    def filter_out_grayscale(example):
        img_tensor = transform(example['image'])
        # Check if the image has only one channel (grayscale)
        if img_tensor.shape[0] == 3:
            return True
        return False


    dataset_val = dataset['validation']
    dataset_val = dataset_val.filter(filter_out_grayscale, num_proc=10)


    num_tasks = utils.get_world_size()
    global_rank = utils.get_rank()
  
    if args.dist_eval:
        if len(dataset_val) % num_tasks != 0:
            logger.info('Warning: Enabling distributed evaluation with an eval dataset not divisible by process number. '
                    'This will slightly alter validation results as extra duplicate entries are added to achieve '
                    'equal num of samples per-process.')
        sampler_val = torch.utils.data.DistributedSampler(
            dataset_val, num_replicas=num_tasks, rank=global_rank, shuffle=False)
    else:
        sampler_val = torch.utils.data.SequentialSampler(dataset_val)

    # leveraging MultiEpochsDataLoader for faster data loading

    args.batch_size = 16 

    data_loader_val = MultiEpochsDataLoader(
        dataset_val, sampler=sampler_val,
        batch_size=int(1 * args.batch_size),
        num_workers=args.num_workers,
        pin_memory=False,
        collate_fn=lambda batch: process_image(batch, build_transform(is_train=False, args=args)),
        drop_last=False
    )

    df = pd.DataFrame()
    model_dict = {
        'vit_base_patch16_mae': 'ViT-B-16',
        'vit_large_patch16_mae':'ViT-L-16',
        'vit_huge_patch14_mae':'ViT-H-14',
        'deit_tiny_patch16_224':"DeiT-B-16",
        'deit_small_patch16_224':"DeiT-B-16",
        'deit_base_patch16_224':"DeiT-B-16",

    }
    k_dict = {
        'vit_base_patch16_mae': [5, 8, 12, 14],
        'vit_large_patch16_mae': [4,6, 9, 10],
        'vit_huge_patch14_mae': [5, 8, 10, 12],
        'deit_tiny_patch16_224': [6, 8, 12, 13],
        'deit_small_patch16_224': [6, 8, 12, 13],
        'deit_base_patch16_224': [5, 8, 12, 14],
        
    }
    for use_k in [False, True]:
        args.use_k = use_k 
    
        for model_ckt in [
            'vit_huge_patch14_mae',
            'vit_large_patch16_mae',
            'vit_base_patch16_mae',
            # 'deit_base_patch16_224',
            # 'deit_small_patch16_224',
            # 'deit_tiny_patch16_224',
        ]:
            for algo in [
                # 'baseline',
                # 'DiffRate',
                # 'PiToMe',
                'ToMe',
            ]:
                # wandb.init(
                #     name=f'{algo}_{model_name_dict[model_ckt]}',
                #     project='ic_off_the_shell',
                #     config={
                #        'algo': algo, 
                #        'model': model_name_dict[model_ckt], 
                #     },
                #     reinit=True
                # )
                args.model = model_ckt
                logger.info(f"Creating model: {args.model}")
                if not args.use_k: 
                    ks = [0.90, 0.925, 0.95, 0.975] if algo != 'baseline' else [1.0]
                else:
                    ks = [0] if algo == 'baseline' else k_dict[model_ckt] 

                # for ratio in ratios:
                model = None
                torch.cuda.empty_cache()
                model = create_model(
                    args.model,
                    pretrained=True,
                    num_classes=1000,
                    drop_rate=args.drop,
                    drop_path_rate=args.drop_path,
                    drop_block_rate=None,
                )
                model = model.to(device)
                if algo == 'ToMe':
                    get_tome_model(model, args)
                elif algo == 'PiToMe':
                    get_pitome_model(model, args)
                elif algo == 'DiffRate':
                    get_diffrate_model(model, args)
                else:
                    get_tome_model(model, args)

                for ratio in ks:
                    if algo == 'DiffRate':
                        if not args.use_k:
                            model.init_kept_num_using_ratio(ratio)
                        else:
                            model.init_kept_num_using_r(ratio)
                    else:
                        if not args.use_k:
                            model.ratio = ratio
                        else:
                            model.r = ratio
                    stats = main(args, model,logger)
                    stats['algo']= algo
                    stats['model']=model_dict[model_ckt] 
                    stats['usr_k']=args.use_k 
                    df = pd.concat([df, pd.DataFrame(stats, index=[0])])
                # wandb.log(stats)
    # df.to_csv('mae_ost.csv') 
    # df.to_csv('deit_ost.csv') 
    df.to_csv('mae_use_k.csv') 
    # df.to_csv('deit_use_k.csv') 
                
        
                
