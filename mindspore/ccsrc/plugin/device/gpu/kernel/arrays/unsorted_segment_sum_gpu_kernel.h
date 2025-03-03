/**
 * Copyright 2020-2022 Huawei Technologies Co., Ltd
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

#ifndef MINDSPORE_CCSRC_BACKEND_KERNEL_COMPILER_GPU_ARRAYS_UNSORTED_SEGMENT_SUM_GPU_KERNEL_H_
#define MINDSPORE_CCSRC_BACKEND_KERNEL_COMPILER_GPU_ARRAYS_UNSORTED_SEGMENT_SUM_GPU_KERNEL_H_

#include <utility>
#include <map>
#include <vector>
#include <algorithm>
#include "mindspore/core/abstract/utils.h"
#include "mindspore/ccsrc/kernel/common_utils.h"
#include "plugin/device/gpu/kernel/gpu_kernel.h"
#include "plugin/device/gpu/kernel/gpu_kernel_factory.h"
#include "plugin/device/gpu/kernel/cuda_impl/cuda_ops/unsorted_segment_sum.cuh"

namespace mindspore {
namespace kernel {
class UnsortedSegmentSumGpuKernelMod : public NativeGpuKernelMod {
 public:
  UnsortedSegmentSumGpuKernelMod() {}
  ~UnsortedSegmentSumGpuKernelMod() {}

  bool Init(const BaseOperatorPtr &base_operator, const std::vector<KernelTensorPtr> &inputs,
            const std::vector<KernelTensorPtr> &outputs) override;

  int Resize(const BaseOperatorPtr &base_operator, const std::vector<KernelTensorPtr> &inputs,
             const std::vector<KernelTensorPtr> &outputs,
             const std::map<uint32_t, tensor::TensorPtr> &inputsOnHost) override;

  bool Launch(const std::vector<AddressPtr> &inputs, const std::vector<AddressPtr> &workspace,
              const std::vector<AddressPtr> &outputs, void *stream_ptr) override {
    return kernel_func_(this, inputs, workspace, outputs, stream_ptr);
  }

 protected:
  std::vector<KernelAttr> GetOpSupport() override;
  template <typename T, typename S>
  bool LaunchKernel(const std::vector<AddressPtr> &inputs, const std::vector<AddressPtr> &workspace,
                    const std::vector<AddressPtr> &outputs, void *stream_ptr);
  using UnsortedSegmentSumFunc =
    std::function<bool(UnsortedSegmentSumGpuKernelMod *, const std::vector<kernel::AddressPtr> &,
                       const std::vector<kernel::AddressPtr> &, const std::vector<kernel::AddressPtr> &, void *)>;
  UnsortedSegmentSumFunc kernel_func_;
  static std::vector<std::pair<KernelAttr, UnsortedSegmentSumFunc>> func_list_;

 private:
  void ResetResource();
  void InitSizeLists();

 private:
  size_t input_dim0_ = 1;
  size_t input_dim1_ = 1;
  size_t output_dim0_ = 1;
  size_t output_dim1_ = 1;
  size_t data_unit_size_ = 0; /* size of T */
  size_t ids_unit_size_ = 0;  /* size of S */
};
}  // namespace kernel
}  // namespace mindspore

#endif  // MINDSPORE_CCSRC_BACKEND_KERNEL_COMPILER_GPU_ARRAYS_UNSORTED_SEGMENT_SUM_GPU_KERNEL_H_
