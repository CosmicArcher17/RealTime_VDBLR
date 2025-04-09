import torch
import torch.nn as nn
import torch.nn.functional as F
import collections
import numpy as np
from models.archs.correlation_package.correlation import Correlation
from models.utils import FM
import torch.nn.utils.weight_norm as wn

import time
import cv2
import copy

class HG(nn.Module):

    def __init__(self, ch_in=9, ch_out=3, ch=32, RB_num=6):
        super(HG, self).__init__()

        self.RB_num = RB_num

        ###################
        #     Encoder
        ###################
        self.enc1 = nn.Sequential(
            nn.Conv2d(ch_in, ch, 5, stride = 1, padding = 2),
            nn.ReLU(),
            # nn.BatchNorm2d(ch),
        )
        self.enc2 = nn.Sequential(
            nn.Conv2d(ch, ch, 3, stride = 2, padding = 1),
            nn.ReLU(),
            # nn.BatchNorm2d(ch),
        )
        self.enc3 = nn.Sequential(
            nn.Conv2d(ch, ch*2, 3, stride = 2, padding = 1),
            nn.ReLU(),
            # nn.BatchNorm2d(ch*2),
        )

        self.RBs = nn.ModuleList([
            nn.Sequential(
                nn.Conv2d(ch*2, ch*2, 3, stride = 1, padding = 1),
                nn.ReLU(),
                # nn.BatchNorm2d(ch*2),
                nn.Conv2d(ch*2, ch*2, 3, stride = 1, padding = 1),
                nn.ReLU(),
                # nn.BatchNorm2d(ch*2),
                ) for i in range(self.RB_num)
            ])
        self.RB_end = nn.Sequential(
            nn.Conv2d(ch*2, ch*2, 3, stride = 1, padding = 1),
            nn.ReLU(),
            # nn.BatchNorm2d(ch*2),
        )

        ###################
        #     Decoder
        ###################
        self.dec1 = nn.Sequential(
            nn.ConvTranspose2d(ch*2, ch, 4, stride = 2, padding = 1),
            nn.ReLU(),
            # nn.BatchNorm2d(ch),
            # nn.Upsample(scale_factor = 2, mode='nearest'),
            # nn.ReflectionPad2d(1),
            # nn.Conv2d(ch*2, ch, kernel_size=3, stride=1, padding=0),
            # nn.ReLU(),
        )
        self.dec2 = nn.Sequential(
            nn.ConvTranspose2d(ch, ch_out, 4, stride = 2, padding = 1),
            nn.ReLU(),
            # nn.BatchNorm2d(ch_out),
            # nn.Upsample(scale_factor = 2, mode='nearest'),
            # nn.ReflectionPad2d(1),
            # nn.Conv2d(ch, ch_out, kernel_size=3, stride=1, padding=0),
            # nn.ReLU(),
        )

    def forward(self, inp):
        # encoder
        f1 = self.enc1(inp)
        f2 = self.enc2(f1)
        n = self.enc3(f2)

        # residual block
        n_ = n.clone()
        for i in range(self.RB_num):
            nn = self.RBs[i](n)
            n = n + nn
        n = self.RB_end(n)
        n = n_ + n

        # decoder
        n = self.dec2((self.dec1(n) + f2)) + f1

        return n

class Network(nn.Module):

    def __init__(self, config, is_distill):
        super(Network, self).__init__()
        self.config = config
        self.rank = torch.distributed.get_rank() if config.dist else -1
        self.is_distill = is_distill
        self.skip_corr_index = config.skip_corr_index

        ch = self.config.ch
        self.RB_num = self.config.RB_num
        self.HG_num = self.config.HG_num
        max_displacement = self.config.max_displacement

        self.pool = torch.nn.AvgPool2d(5, stride=1, padding=2)
        self.corr = Correlation(pad_size=max_displacement, kernel_size=1, max_displacement=max_displacement, stride1=1, stride2=1)

        self.base_conv = nn.Sequential(
            nn.Conv2d(3, ch, 3, stride=1, padding=1),
            nn.ReLU(),
            # nn.BatchNorm2d(ch),
        )

        self.inp_prev_conv = nn.Sequential(
            nn.Conv2d(15, ch, 3, stride=1, padding=1),
            nn.ReLU(),
            # nn.BatchNorm2d(ch),
        )

        self.HGs = nn.ModuleList(
            [HG(ch_in=ch*2, ch_out=ch, ch=ch, RB_num=self.RB_num) for i in range(self.HG_num)]
        )
        self.motion_layer = nn.ModuleList(
            [nn.Sequential(
                nn.Conv2d(ch, ch*2, 5, stride=4, padding=1),
                nn.ReLU()) for i in range(self.HG_num - len(self.skip_corr_index))]
        )

        if is_distill is True:
            for name, param in self.HGs[self.HG_num - 1].named_parameters():
                param.require_grad = False
        elif is_distill is False:
            for i in range(self.HG_num - len(self.skip_corr_index)):
                for name, param in self.motion_layer[i].named_parameters():
                    param.require_grad = False

            self.deblur_layer = nn.ModuleList(
                [nn.Conv2d(ch*2, 3, 3, stride=1, padding=1) for i in range(self.HG_num - 1)]
            )
            self.out = nn.Conv2d(ch, 3, 3, stride=1, padding=1)
        elif is_distill is None:
            self.out = nn.Conv2d(ch, 3, 3, stride=1, padding=1)

        self.pre_f_l_prev = None
        self.post_f_prev = None

    def forward(self, I_prev_prev, I_prev, I_curr, I_next, I_next_next, R_prev, is_first_frame,use_weights=True):
        def compute_sobel_weight_map(tensor_img):
            # Assumes input shape: [B, C, H, W]
            b, c, h, w = tensor_img.shape
            img_np = tensor_img[0].permute(1, 2, 0).detach().cpu().numpy()  # (H, W, C)
            img_gray = cv2.cvtColor((img_np * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
        
            # Sobel edge detection
            sobelx = cv2.Sobel(img_gray, cv2.CV_64F, 1, 0, ksize=3)
            sobely = cv2.Sobel(img_gray, cv2.CV_64F, 0, 1, ksize=3)
            sobel_magnitude = np.sqrt(sobelx ** 2 + sobely ** 2)
        
            # Return mean edge strength as sharpness score
            return np.mean(sobel_magnitude)
        pre_f_l, corr_l, flow_l = [], [], []
        aux_l = []
        motion_layer_index = list(range(self.HG_num - len(self.skip_corr_index)))
        # if use_weights:
        #     with torch.no_grad():
        #         frames = [I_prev_prev, I_prev, I_curr, I_next, I_next_next]
        #         weights = []
        #         for f in frames:
        #             weights.append(compute_sobel_weight_map(f))
            
        #         weights = torch.tensor(weights, device=I_curr.device, dtype=I_curr.dtype)
        #         weights = weights / weights.sum()  # normalize to sum to 1
            
        #         # Apply weights to each frame
        #         I_prev_prev = I_prev_prev * weights[0]
        #         I_prev = I_prev * weights[1]
        #         I_curr = I_curr * weights[2]
        #         I_next = I_next * weights[3]
        #         I_next_next = I_next_next * weights[4]

        # else:
        #     pass
        base = self.base_conv(I_curr)
        inp_prev = self.inp_prev_conv(torch.cat((I_prev_prev, I_prev, R_prev, I_next, I_next_next), axis = 1))
        n = torch.cat((base, inp_prev), axis = 1)
        for i in range(self.HG_num):
            ## MTU
            n_ = self.HGs[i](n)

            ## motion layer
            # assert i==0 and i not in self.skip_corr_index
            if i not in self.skip_corr_index:
                pre_f = self.motion_layer[motion_layer_index.pop(0)](base + n_)
            else:
                pre_f = None

            pre_f_prev = self.pre_f_l_prev[i] if is_first_frame is False else pre_f
            post_f_prev = self.post_f_prev if is_first_frame is False else n_

            ## motion compensation
            flow_prev = None if i not in self.skip_corr_index else self.flow_prev
            post_f_prev_warped, corr, flow = FM(pre_f, pre_f_prev, post_f_prev, self.corr, self.pool, flow_prev=flow_prev, scale=4)

            if i not in self.skip_corr_index:
                self.corr_prev = corr
                self.flow_prev = flow
            else:
                corr = self.corr_prev

            n = torch.cat((n_, post_f_prev_warped), axis = 1)

            ## deblur layer
            if i < self.HG_num - 1:
                if self.is_distill is False:
                    aux = self.deblur_layer[i](n) + I_curr
                    aux_l.append(aux.clone())

            pre_f_l.append(pre_f.clone().detach() if pre_f is not None else None)
            flow_l.append(flow.clone().detach())

            if self.is_distill:
                if i not in self.skip_corr_index:
                    corr_l.append(torch.unsqueeze(corr.clone(), 2))

        if self.is_distill is False or self.is_distill is None:
            n = self.out(n_)
            n = I_curr + n

        self.pre_f_l_prev = pre_f_l
        self.post_f_prev = n_.clone().detach()


        outs = collections.OrderedDict()
        if self.is_distill is False:
            outs['result'] = n
            if self.HG_num > 1:
                outs['aux_l'] = torch.cat(aux_l, axis = 1)
            else:
                outs['aux_l'] = None
        elif self.is_distill is True:
            outs['corr_l'] = torch.cat(corr_l, axis = 2)
        elif self.is_distill is None:
            outs['result'] = n

        # end
        if self.config.save_sample:
            outs['pre_f'] = pre_f.clone().detach() if pre_f is not None else None
            outs['pre_f_prev'] = pre_f_prev.clone().detach() if pre_f_prev is not None else None
            outs['post_f'] = n_.clone().detach()
            outs['post_f_prev'] = post_f_prev.clone().detach()
            outs['post_f_prev_warped'] = post_f_prev_warped.clone().detach()
            outs['flow'] = flow_l[-1]

        return outs
