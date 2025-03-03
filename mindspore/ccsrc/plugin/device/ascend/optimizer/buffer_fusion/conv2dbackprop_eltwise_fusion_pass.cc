/**
 * Copyright 2020-2021 Huawei Technologies Co., Ltd
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
#include "plugin/device/ascend/optimizer/buffer_fusion/conv2dbackprop_eltwise_fusion_pass.h"
#include "kernel/kernel_fusion.h"
#include "backend/common/session/anf_runtime_algorithm.h"
#include "include/common/utils/anfalgo.h"
#include "mindspore/core/ops/core_ops.h"
#include "utils/ms_context.h"
#include "backend/common/optimizer/fusion_id_allocator.h"

namespace mindspore {
namespace opt {
void Conv2DBackpropEltwiseFusionPass::MatchConv2DBackpropInputEltwise(const CNodePtr &cnode,
                                                                      const session::KernelGraph &kernel_graph,
                                                                      FusedNodeRecord *candidate_fusion) {
  MS_EXCEPTION_IF_NULL(cnode);
  MS_EXCEPTION_IF_NULL(candidate_fusion);
  mindspore::HashSet<AnfNodePtr> record{cnode};
  auto eltwise_input = cnode->input(kIndex1);
  MS_EXCEPTION_IF_NULL(eltwise_input);
  if (!eltwise_input->isa<CNode>() || !AnfUtils::IsRealCNodeKernel(eltwise_input) ||
      fusion_id_allocator->HasFusionIdAttr(eltwise_input)) {
    return;
  }
  if (common::AnfAlgo::CheckPrimitiveType(eltwise_input, prim::kPrimConv2DBackpropInput)) {
    (void)record.insert(eltwise_input);
    candidate_fusion->push_back(record);
    SetRecordFusionId(record);
  }
}

void Conv2DBackpropEltwiseFusionPass::MatchSingleFusionPattern(const session::KernelGraph &kernel_graph,
                                                               FusedNodeRecord *candidate_fusion) {
  MS_EXCEPTION_IF_NULL(candidate_fusion);
  const auto &node_list = TopoSort(kernel_graph.get_return());
  for (auto &node : node_list) {
    if (!AnfUtils::IsRealCNodeKernel(node) || fusion_id_allocator->HasFusionIdAttr(node) ||
        common::AnfAlgo::CheckPrimitiveType(node, prim::kPrimReturn)) {
      continue;
    }
    auto cnode = node->cast<CNodePtr>();
    MS_EXCEPTION_IF_NULL(cnode);
    if (common::AnfAlgo::GetCNodeName(cnode) == kReluGradV2OpName) {
      MatchConv2DBackpropInputEltwise(cnode, kernel_graph, candidate_fusion);
    }
  }
}
}  // namespace opt
}  // namespace mindspore
