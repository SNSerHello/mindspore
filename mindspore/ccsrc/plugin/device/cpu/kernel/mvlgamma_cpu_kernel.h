/**
 * Copyright 2022 Huawei Technologies Co., Ltd
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

#ifndef MINDSPORE_CCSRC_BACKEND_KERNEL_COMPILER_CPU_MVLGAMMA_CPU_KERNEL_H_
#define MINDSPORE_CCSRC_BACKEND_KERNEL_COMPILER_CPU_MVLGAMMA_CPU_KERNEL_H_
#include <functional>
#include <memory>
#include <vector>
#include "plugin/device/cpu/kernel/cpu_kernel.h"
#include "plugin/factory/ms_factory.h"

namespace mindspore {
namespace kernel {
class MvlgammaCpuKernelMod : public DeprecatedNativeCpuKernelMod {
 public:
  MvlgammaCpuKernelMod() = default;
  ~MvlgammaCpuKernelMod() override = default;

  void InitKernel(const CNodePtr &kernel_node) override;

  template <typename T>
  T MvlgammaSingle(const T &x, const int64_t &p);

  bool Launch(const std::vector<AddressPtr> &inputs, const std::vector<AddressPtr> &workspace,
              const std::vector<AddressPtr> &outputs) override;

  template <typename T>
  bool LaunchKernel(const std::vector<AddressPtr> &inputs, const std::vector<AddressPtr> &outputs);

 private:
  ShapeVector input_shape_;
  ShapeVector output_shape_;
  int64_t attr_p_;
  int64_t input_tensor_size_;
  TypeId dtype_{kTypeUnknown};

 protected:
  std::vector<KernelAttr> GetOpSupport() override;
};
}  // namespace kernel
}  // namespace mindspore
#endif  // MINDSPORE_CCSRC_BACKEND_KERNEL_COMPILER_CPU_MVLGAMMA_CPU_KERNEL_H_
