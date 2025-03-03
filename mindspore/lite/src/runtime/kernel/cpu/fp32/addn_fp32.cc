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
#include "src/runtime/kernel/cpu/fp32/addn_fp32.h"
#include "src/runtime/kernel_registry.h"
#include "nnacl/fp32/arithmetic_fp32.h"
#include "include/errorcode.h"

using mindspore::kernel::KERNEL_ARCH;
using mindspore::lite::KernelRegistrar;
using mindspore::lite::RET_ERROR;
using mindspore::lite::RET_NULL_PTR;
using mindspore::lite::RET_OK;
using mindspore::schema::PrimitiveType_AddN;

namespace mindspore::kernel {
namespace {
int AddNLaunch(void *cdata, int task_id, float lhs_scale, float rhs_scale) {
  if (cdata == nullptr) {
    MS_LOG(ERROR) << "Input cdata is nullptr!";
    return RET_NULL_PTR;
  }
  auto kernel = reinterpret_cast<AddNCPUKernel *>(cdata);
  return kernel->AddNParallelRun(task_id, lhs_scale, rhs_scale);
}
}  // namespace

int AddNCPUKernel::Prepare() {
  CHECK_LESS_RETURN(in_tensors_.size(), C2NUM);
  CHECK_LESS_RETURN(out_tensors_.size(), 1);
  MS_CHECK_FALSE(in_tensors_[0]->shape().empty(), RET_ERROR);
  MS_CHECK_FALSE(in_tensors_[1]->shape().empty(), RET_ERROR);
  return RET_OK;
}

int AddNCPUKernel::ReSize() { return RET_OK; }

int AddNCPUKernel::AddNParallelRun(int thread_id, float lhs_scale, float rhs_scale) {
  int count_per_thread = UP_DIV(elements_num_, op_parameter_->thread_num_);
  int count = MSMIN(count_per_thread, elements_num_ - thread_id * count_per_thread);
  auto stride = count_per_thread * thread_id;
  auto ret = ElementAdd(in1_addr_ + stride, in2_addr_ + stride, out_addr_ + stride, count);
  if (ret != NNACL_OK) {
    MS_LOG(ERROR) << "ElementAdd fail! ret: " << ret;
    return RET_ERROR;
  }
  return RET_OK;
}

int AddNCPUKernel::Run() {
  elements_num_ = out_tensors_[0]->ElementsNum();
  auto input0_data = reinterpret_cast<float *>(in_tensors_[0]->MutableData());
  auto input1_data = reinterpret_cast<float *>(in_tensors_[1]->MutableData());
  auto output_data = reinterpret_cast<float *>(out_tensors_[0]->MutableData());
  CHECK_NULL_RETURN(input0_data);
  CHECK_NULL_RETURN(input1_data);
  CHECK_NULL_RETURN(output_data);
  if (static_cast<int>(elements_num_) < op_parameter_->thread_num_) {
    if (in_tensors_[0]->shape() == in_tensors_[1]->shape()) {
      ElementAdd(input0_data, input1_data, output_data, elements_num_);
    } else {
      ArithmeticParameter param;
      param.in_elements_num0_ = in_tensors_[0]->ElementsNum();
      param.in_elements_num1_ = in_tensors_[1]->ElementsNum();
      param.out_elements_num_ = out_tensors_[0]->ElementsNum();
      param.broadcasting_ = true;
      ElementOptAdd(input0_data, input1_data, output_data, elements_num_, &param);
    }

    for (size_t i = 2; i < in_tensors_.size(); ++i) {
      auto in_data = reinterpret_cast<float *>(in_tensors_[i]->MutableData());
      CHECK_NULL_RETURN(in_data);
      if (in_tensors_[i]->shape() == out_tensors_[0]->shape()) {
        ElementAdd(in_data, output_data, output_data, elements_num_);
      } else {
        ArithmeticParameter param;
        param.in_elements_num0_ = in_tensors_[i]->ElementsNum();
        param.in_elements_num1_ = out_tensors_[0]->ElementsNum();
        param.out_elements_num_ = out_tensors_[0]->ElementsNum();
        param.broadcasting_ = true;
        ElementOptAdd(in_data, output_data, output_data, elements_num_, &param);
      }
    }
    return RET_OK;
  }
  if (std::any_of(in_tensors_.begin(), in_tensors_.end(),
                  [this](const lite::Tensor *input) { return input->shape() != out_tensors_.at(0)->shape(); })) {
    MS_LOG(ERROR) << "all inputs should have the same shape of output.";
    return RET_ERROR;
  }
  in1_addr_ = input0_data;
  in2_addr_ = input1_data;
  out_addr_ = output_data;
  auto ret = ParallelLaunch(this->ms_context_, AddNLaunch, this, op_parameter_->thread_num_);
  if (ret != RET_OK) {
    MS_LOG(ERROR) << "addn launch fail!ret: " << ret;
    return RET_ERROR;
  }
  for (size_t i = 2; i < in_tensors_.size(); ++i) {
    in1_addr_ = reinterpret_cast<float *>(in_tensors_[i]->MutableData());
    CHECK_NULL_RETURN(in1_addr_);
    in2_addr_ = output_data;
    ret = ParallelLaunch(this->ms_context_, AddNLaunch, this, op_parameter_->thread_num_);
    if (ret != RET_OK) {
      MS_LOG(ERROR) << "addn launch fail!ret: " << ret << ", input index: " << i;
      return RET_ERROR;
    }
  }
  return RET_OK;
}

REG_KERNEL(kCPU, kNumberTypeFloat32, PrimitiveType_AddN, LiteKernelCreator<AddNCPUKernel>)
}  // namespace mindspore::kernel
