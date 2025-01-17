#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2023 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from ut_utils import *


@capture_args
def test(m, n, data_type, p,  dump_info=False):
    weight = torch.rand(m, n, dtype=torch.float)
    grad = torch.rand(m, n, dtype=torch.float)
    if data_type == "bf16":
        weight = weight.to(torch.bfloat16)
        grad = grad.to(torch.bfloat16)
    bk_grad = grad.clone()
    mask = torch.ops.qbits_customop.qbits_dropout_fwd(weight, p)
    num_zero = (m*n-torch.nonzero(mask.reshape(-1)).numel())
    dropout_p = num_zero/(m*n)
    if dump_info:
        print("input p:"+str(p))
        print("dropout p:"+str(dropout_p))
    if not torch.allclose(torch.tensor(p), torch.tensor(dropout_p), 0.03):
        print("fail")
    torch.ops.qbits_customop.qbits_dropout_bwd(grad, mask)
    bk_grad = torch.mul(bk_grad, mask)
    if dump_info:
        print(grad)
        print(bk_grad)
    if torch.allclose(grad, bk_grad, 0.01):
        print("ok")
    else:
        print("fail")


m_list = [151, 11008]
n_list = [87, 4096]
dt_list = ["fp32", "bf16"]
p_list = [0.2, 0.8]

for m in m_list:
    for n in n_list:
        for dt in dt_list:
            for p in p_list:
                test(m, n, dt, p)
