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

#ifndef MINDSPORE_LITE_SRC_RUNTIME_KERNEL_CPU_FP32_MATMUL_FP32_BASE_H_
#define MINDSPORE_LITE_SRC_RUNTIME_KERNEL_CPU_FP32_MATMUL_FP32_BASE_H_

#include <vector>
#include "src/runtime/lite_kernel.h"
#include "src/runtime/pack_weight_manager.h"
#include "nnacl/matmul_parameter.h"
#include "include/errorcode.h"
#include "src/common/common.h"

using mindspore::lite::RET_ERROR;
using mindspore::lite::RET_MEMORY_FAILED;
using mindspore::lite::RET_OK;

namespace mindspore::kernel {
using MatrixPackFun = void (*)(const float *src_ptr, float *dst_ptr, int row, int col);
using GemmIsNotPackFun = void (*)(const float *a, const float *b, float *c, const float *bias, int m, int k,
                                  int act_type);

class MatmulFp32BaseCPUKernel : public LiteKernel {
 public:
  MatmulFp32BaseCPUKernel(OpParameter *parameter, const std::vector<lite::Tensor *> &inputs,
                          const std::vector<lite::Tensor *> &outputs, const mindspore::lite::InnerContext *ctx)
      : LiteKernel(parameter, inputs, outputs, ctx) {
    params_ = reinterpret_cast<MatMulParameter *>(op_parameter_);
  }
  ~MatmulFp32BaseCPUKernel() override;
  int Prepare() override;
  int FullConnectionPrepare();
  int MatmulPrepare();
  int ReSize() override;
  int FullConnectionReSize();
  int MatmulReSize();
  int Run() override;

  using ParallelRun = int (MatmulFp32BaseCPUKernel::*)(int task_id) const;
  ParallelRun parallel_fun_ = nullptr;

 private:
  struct MatrixInfo {
    bool need_pack{false};
    bool has_packed{false};  // only valid for constant, only do once throughout the process.
    bool has_origin{false};  // only valid for constant, only true when failing to infer shape, then false after packed.
    int pack_size{-1};
    float *origin_ptr{nullptr};  // only valid for constant, which is synchronized with the 'has_origin'.
    float *pack_ptr{nullptr};
  };

  virtual int ParallelRunByRow(int task_id) const;
  virtual int ParallelRunByOC(int task_id) const;
  virtual int ParallelRunByBatch(int task_id) const;
  int ParallelRunIsNotPackByBatch(int task_id) const;
  int BackupConstMatrix(MatrixInfo *matrix_info, int index);
  virtual void InitGlobalVariable();
  int PackMatrixA();
  int PackMatrixB();
  int PackMatrixAImpl();
  int PackMatrixBImpl();
  virtual int PackMatrixAImplOpt();
  bool CheckRow1OptimalConditions();
  virtual bool SupportMulBatchCuttingByRow() { return false; }
  int PackBiasMatrix();
  void FreePackedMatrixA();
  void FreePackedMatrixB();
  int InitParameter();
  int InitTmpOutBuffer();
  int GetThreadCuttingPolicy();
  virtual bool CheckThreadCuttingByRow();
  void GetThreadCuttingInfoByRow();
  void InitShapeA();
  void InitShapeB();
  int InitBroadcastParams();

 protected:
  MatMulParameter *params_ = nullptr;
  GemmIsNotPackFun gemmIsNotPackFun = nullptr;
  int a_batch_ = 1;
  int b_batch_ = 1;
  std::vector<int> a_offset_;
  std::vector<int> b_offset_;

  int col_tile_ = 0;
  int row_tile_ = 0;
  int batch_stride_ = 0;
  int row_num_;
  int row_min_unit_{1};
  int col_min_unit_{1};
  int thread_count_ = 0;
  float *output_data_ = nullptr;
  bool out_need_aligned_ = false;
  int col_step_ = 0;
  std::vector<int> split_points_;
  MatrixInfo matrix_a_;
  MatrixInfo matrix_b_;
  MatrixInfo matrix_c_;
  bool pack_opt_{false};  // indicate whether packing can be multi-threads, currently, only support in ARM64 && packA.
  MatrixPackFun matrix_a_pack_fun_ = nullptr;
  MatrixPackFun matrix_b_pack_fun_ = nullptr;
};
}  // namespace mindspore::kernel
#endif  // MINDSPORE_LITE_SRC_RUNTIME_KERNEL_CPU_FP32_MATMUL_FP32_BASE_H_
