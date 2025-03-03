/**
 * Copyright 2020 Huawei Technologies Co., Ltd
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

#include "plugin/device/ascend/kernel/host/dynamic_shape_kernel.h"
#include "backend/common/session/anf_runtime_algorithm.h"
#include "include/common/utils/anfalgo.h"
#include "utils/trace_base.h"
#include "plugin/device/ascend/hal/device/ascend_kernel_runtime.h"
#include "runtime/mem.h"

namespace mindspore {
namespace kernel {
void TensorShapeKernelMod::Execute() const {
  MS_LOG(INFO) << "Execute TensorShapeKernel Start";
  auto node = anf_node_.lock();
  MS_EXCEPTION_IF_NULL(node);
  auto cnode = node->cast<CNodePtr>();
  MS_EXCEPTION_IF_NULL(cnode);
  auto input_num = common::AnfAlgo::GetInputTensorNum(cnode);
  if (input_num != 1) {
    MS_LOG(EXCEPTION) << "Op [" << cnode->DebugString() << "] has invalid input num, should be 1, but got " << input_num
                      << trace::DumpSourceLines(cnode);
  }

  auto prev_output_shape = common::AnfAlgo::GetPrevNodeOutputInferShape(cnode, 0);
  std::vector<int64_t> output_shape = {SizeToLong(prev_output_shape.size())};

  auto output_type = TypeId::kNumberTypeInt64;

  auto output_tensor_for_sync = std::make_shared<tensor::Tensor>(output_type, output_shape);
  MS_EXCEPTION_IF_NULL(output_tensor_for_sync);
  auto data_ptr = static_cast<int64_t *>(output_tensor_for_sync->data_c());
  for (size_t i = 0; i < prev_output_shape.size(); ++i) {
    MS_LOG(INFO) << "DEBUG prev_output_shape[" << i << "]:" << prev_output_shape[i];
    *(data_ptr + i) = prev_output_shape[i];
  }

  auto output_addr = AnfAlgo::GetOutputAddr(cnode, 0);
  MS_EXCEPTION_IF_NULL(output_addr);

  if (output_addr->GetDeviceType() == device::DeviceType::kCPU) {
    auto ret = memcpy_s(const_cast<void *>(output_addr->GetPtr()), output_addr->GetSize(),
                        output_tensor_for_sync->data_c(), LongToSize(output_tensor_for_sync->data().nbytes()));
    if (ret != EOK) {
      MS_LOG(EXCEPTION) << "Execute TensorShapeKernel memcpy_s failed!";
    }
  } else {
    auto runtime_instance = device::KernelRuntimeManager::Instance().GetCurrentKernelRuntime();
    MS_EXCEPTION_IF_NULL(runtime_instance);
    auto ret = runtime_instance->SyncStream();
    if (!ret) {
      MS_LOG(EXCEPTION) << "Sync stream error!";
    }
    if (!output_addr->SyncHostToDevice(output_shape, LongToSize(output_tensor_for_sync->data().nbytes()),
                                       output_tensor_for_sync->data_type(), output_tensor_for_sync->data_c(),
                                       output_tensor_for_sync->device_info().host_format_)) {
      MS_LOG(EXCEPTION) << "TensorShapeKernel SyncHostToDevice failed.";
    }
  }

  MS_LOG(INFO) << "Execute TensorShapeKernel End";
}

bool TensorShapeKernelMod::Launch(const std::vector<AddressPtr> &, const std::vector<AddressPtr> &,
                                  const std::vector<AddressPtr> &, void *stream_ptr) {
  auto node = anf_node_.lock();
  MS_EXCEPTION_IF_NULL(node);
  auto cnode = node->cast<CNodePtr>();
  MS_EXCEPTION_IF_NULL(cnode);
  if (stream_ == nullptr) {
    stream_ = stream_ptr;
  }
  try {
    Execute();
  } catch (const std::exception &e) {
    MS_LOG(ERROR) << "TensorShapeKernelMod Launch failed. node: " << cnode->fullname_with_scope()
                  << ", Error message is " << e.what();
    return false;
  }
  return true;
}
}  // namespace kernel
}  // namespace mindspore
