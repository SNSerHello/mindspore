/**
 * Copyright 2019-2022 Huawei Technologies Co., Ltd
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
#ifndef MINDSPORE_CCSRC_BACKEND_KERNEL_COMPILER_KERNEL_H_
#define MINDSPORE_CCSRC_BACKEND_KERNEL_COMPILER_KERNEL_H_
#include <vector>
#include <string>
#include <memory>
#include <map>
#include <set>
#include "nlohmann/json.hpp"
#include "ir/anf.h"
#include "ir/dtype.h"
#include "include/common/utils/utils.h"
#include "mindspore/core/ops/base_operator.h"
#include "ir/tensor.h"
#include "abstract/dshape.h"
#include "utils/log_adapter.h"
#include "abstract/ops/primitive_infer_map.h"
#include "include/api/format.h"

#ifdef _MSC_VER
#undef OPAQUE
#endif

namespace mindspore {
enum KernelType : int {
  UNKNOWN_KERNEL_TYPE = 0,
  AKG_KERNEL,
  AICPU_KERNEL,
  RT_KERNEL,
  HCCL_KERNEL,
  TBE_KERNEL,
  HOST_KERNEL,
  CPU_KERNEL,
  GPU_KERNEL,
};
namespace kernel {
// Supported fusion type
enum FusionType {
  CONV = 0,
  ELEMWISE,
  COMMREDUCE,
  SEGMENT,
  OPAQUE,
  BN_UPDATE_GRAD,
  BN_GRAD_REDUCE,
  LAYER_NORM_GRAD,
  L2LOSS_MUL_ADDN,
  PURE_BROADCAST,
  INPLACE,
  MATMUL,
  MATMUL_V2,
  GEMM,
  CONV2D_BACKPROP_INPUT,
  CONV2D_BACKPROP_FILTER,
  CONV3D_BACKPROP_INPUT,
  CONV3D_BACKPROP_FILTER,
  CUBE_LAYER_NORM,
  BN_REDUCE,
  BN_UPDATE,
  SOFTMAX_CROSS_ENTROPY_WITH_LOGITS,
  L2_NORMALIZE,
  SOFTMAX,
  L2_LOSS,
  ASCEND_QUANT,
  ASCEND_DEQUANT,
  ASCEND_ANTI_QUANT,
  STRIDED_READ,
  STRIDED_WRITE,
  ASCEND_DEQUANT_S16,
  ASCEND_REQUANT,
  ASCEND_REQUANT_S16,
  MAX_POOL,
  DEPTHWISECONV,
  CONV3D,
  POOL2D,
  POOL3D,
  READ_SELECT,
  WRITE_SELECT,
  COSINE_EMBEDDING_LOSS,
  DILATION_PATTERN,
  BROAD_CAST,
  BATCH_MATMUL,
  CONFUSION_TRANSPOSE,
  DROPOUT_DOMASKV3D,
  UNKNOWN_FUSION_TYPE = -1,
};

enum OpPattern {
  kCommonPattern = 0,
  kFormatAgnosticPattern = 1,
  kBroadcastPattern = 2,
  kReducePattern = 3,
};

// Backend processor
enum Processor {
  UNKNOWN = -1,
  AICORE = 0,
  AICPU,
  CUDA,
  CPU,
};

struct FlexArray {
  size_t len;
  char contents[];
};

struct KernelJsonInfo {
  std::string bin_file_name;
  std::string bin_file_suffix;
  uint32_t block_dim;
  std::string kernel_name;
  std::string magic;
  std::vector<size_t> parameters;
  std::string sha256;
  std::vector<size_t> workspaces;
  bool has_kernel_list = false;
  uint32_t op_para_size;
  KernelJsonInfo() : block_dim(0), op_para_size(0) {}
};

class KernelPack {
 public:
  KernelPack() : json_(nullptr), kernel_(nullptr) {}
  KernelPack(const KernelPack &) = default;
  KernelPack &operator=(const KernelPack &) = default;
  KernelJsonInfo kernel_json_info() const;
  bool LoadKernelMeta(const std::string &json_f);
  bool ReadFromJsonFile(const std::string &json_f, const std::string &processor);
  const FlexArray *GetJson() const { return json_; }
  const FlexArray *GetKernel() const { return kernel_; }
  ~KernelPack() {
    if (json_ != nullptr) {
      delete[] json_;
      json_ = nullptr;
    }
    if (kernel_ != nullptr) {
      delete[] kernel_;
      kernel_ = nullptr;
    }
  }

 private:
  bool ReadFromJsonFileHelper(std::ifstream &kernel_bin);
  void ParseKernelJson(const nlohmann::json &js);
  KernelJsonInfo kernel_json_info_;
  FlexArray *json_;
  FlexArray *kernel_;
};
using KernelPackPtr = std::shared_ptr<KernelPack>;

/**
 * @brief base class for autotensor kernel and cce kernel.
 */
struct Address {
  Address() : addr(nullptr), size(0) {}
  Address(void *address_addr, size_t address_size) : addr(address_addr), size(address_size) {}
  void *addr;
  size_t size;
};
using AddressPtr = std::shared_ptr<Address>;
using AddressPtrList = std::vector<AddressPtr>;
using StreamType = void *;
// The memory info of kernel launch.
struct KernelLaunchInfo {
  AddressPtrList inputs_;
  AddressPtrList outputs_;
  AddressPtrList workspaces_;
};
struct TensorInfo {
  mindspore::Format format;
  abstract::AbstractBasePtr abstract_base;       // Store data type and shape.
  std::vector<int64_t> device_shape_adaptively;  // deprecated field for dynamic shape
};
using TensorInfoPtr = std::shared_ptr<TensorInfo>;
using BaseOperatorPtr = std::shared_ptr<ops::BaseOperator>;

class KernelTensor {
 public:
  KernelTensor() = default;
  ~KernelTensor() = default;

  bool IsDynamicShape() const;
  size_t GetSizeInBytes() const;
  AddressPtr GetData() const { return data_; }
  TypeId GetDtype() const;
  mindspore::Format GetFormat() const { return tensor_info_.format; }
  // If real type is not a list or tuple tensor, it will return kTypeUnknown.
  std::vector<TypeId> GetListOrTupleDtype() const;
  // If real type is not a single shape vector, it will return empty.
  ShapeVector GetShapeVector() const;
  // If real type is not a list or tuple shape vector, it will return empty.
  std::vector<ShapeVector> GetListOrTupleShapeVector() const;
  void SetData(const AddressPtr &data) { data_ = data; }
  void SetDtype(const TypePtr &dtype);
  void SetFormat(mindspore::Format format) { tensor_info_.format = format; }
  void SetShapeVector(const ShapeVector &shape);

  abstract::BaseShapePtr GetBaseShape() const;
  // If the shape need to be List or Tuple, `SetBaseShape` should be called.
  void SetBaseShape(const abstract::BaseShapePtr &base_shape);
  void SetAbstract(const abstract::AbstractBasePtr &base_abstract) { tensor_info_.abstract_base = base_abstract; }
  void SetTensorInfo(const TensorInfo &tensor_info) { tensor_info_ = tensor_info; }

  // deprecated field for dynamic shape
  const ShapeVector &GetDeviceShapeAdaptively() const;
  void SetDeviceShapeAdaptively(const ShapeVector &device_shape_adaptively);

 private:
  TensorInfo tensor_info_;
  AddressPtr data_{nullptr};
  string GetAbstractName() const;
};
using KernelTensorPtr = std::shared_ptr<KernelTensor>;

enum class KernelModType {
  Invalid = 0,
  KernelMod,
  GpuKernelMod,
  NativeGpuKernelMod,
  DeprecatedNativeGpuKernelMod,
  CpuKernelMod,
  NativeCpuKernelMod,
  DeprecatedNativeCpuKernelMod,
  HostKernelMod,
};

enum KernelErrorCode { KRET_OK = 0, KRET_RESIZE_FAILED = 1, KRET_UNKNOWN_SHAPE = 2, KRET_UNKNOWN_OUT_SHAPE = 3 };

class KernelMod {
 public:
  KernelMod() {}
  virtual ~KernelMod() = default;
  virtual void SetInputSizeList(const std::vector<size_t> &size_list) { input_size_list_ = size_list; }
  virtual void SetOutputSizeList(const std::vector<size_t> &size_list) { output_size_list_ = size_list; }
  virtual void SetWorkspaceSizeList(const std::vector<size_t> &size_list) { workspace_size_list_ = size_list; }
  virtual const std::vector<size_t> &GetInputSizeList() const { return input_size_list_; }
  virtual const std::vector<size_t> &GetOutputSizeList() const { return output_size_list_; }
  virtual const std::vector<size_t> &GetWorkspaceSizeList() const { return workspace_size_list_; }
  virtual bool Launch(const std::vector<AddressPtr> &inputs, const std::vector<AddressPtr> &workspace,
                      const std::vector<AddressPtr> &outputs, void *stream_ptr) = 0;
  virtual std::vector<size_t> GenParameters() { return {}; }
  virtual void ReleaseResource() {}
  // Initialization for the kernel mod.
  virtual bool Init(const BaseOperatorPtr & /* base_operator */, const std::vector<KernelTensorPtr> & /* inputs */,
                    const std::vector<KernelTensorPtr> & /* outputs */) {
    return true;
  }

  // Resize is for updating shape related information and performing shape related operation(e.g., calculate output
  // tensor size and allocate output tensor memory).
  // sometimes resize need the input tensor data, framework will sync and retain these tensor data from device to host
  // and pass them by the param inputsOnHost
  virtual int Resize(
    const BaseOperatorPtr &base_operator, const std::vector<KernelTensorPtr> &inputs,
    const std::vector<KernelTensorPtr> &outputs,
    const std::map<uint32_t, tensor::TensorPtr> &inputsOnHost = std::map<uint32_t, tensor::TensorPtr>());

  // Some kernels, e.g., Unique, can only get its output shape after its computing finished.
  virtual bool IsNeedRetrieveOutputShape() { return is_need_retrieve_output_shape_; }
  virtual std::vector<KernelTensorPtr> RetrieveOutputShape() {
    SyncData();
    return GetOutputs();
  }
  void set_unique_name(const std::string &unique_name) { unique_name_ = unique_name; }
  void set_fullname(const std::string &fullname) { fullname_ = fullname; }
  void set_is_monad(bool is_monad) { is_monad_ = is_monad; }
  void set_inputs_addr(const std::vector<AddressPtr> &addr) { inputs_addr_ = addr; }
  void set_workspaces_addr(const std::vector<AddressPtr> &addr) { workspaces_addr_ = addr; }
  void set_outputs_addr(const std::vector<AddressPtr> &addr) { outputs_addr_ = addr; }
  const std::vector<AddressPtr> &GetInputsAddr() const { return inputs_addr_; }
  const std::vector<AddressPtr> &GetWorkSpacesAddr() const { return workspaces_addr_; }
  const std::vector<AddressPtr> &GetOutputsAddr() const { return outputs_addr_; }
  void set_stream(StreamType stream) { stream_ = stream; }
  StreamType stream() const { return stream_; }
  virtual enum KernelModType GetKernelModType() const { return KernelModType::KernelMod; }
  bool Launch(const KernelLaunchInfo &kernel_launch_address, void *stream_ptr) {
    return Launch(kernel_launch_address.inputs_, kernel_launch_address.workspaces_, kernel_launch_address.outputs_,
                  stream_ptr);
  }

 protected:
  virtual void SyncData() {}
  virtual std::vector<KernelTensorPtr> GetOutputs() { return {}; }
  bool IsValidShape(const ShapeVector &shape) const {
    if (std::any_of(shape.begin(), shape.end(), [](int64_t dim) { return dim < 0; })) {
      return false;
    }
    return true;
  }

  std::string kernel_name_;
  std::string unique_name_;
  std::string fullname_;
  bool is_monad_{false};
  StreamType stream_{nullptr};
  std::vector<size_t> input_size_list_;
  std::vector<size_t> output_size_list_;
  std::vector<size_t> workspace_size_list_;
  bool is_need_retrieve_output_shape_ = false;

 private:
  std::vector<AddressPtr> inputs_addr_;
  std::vector<AddressPtr> workspaces_addr_;
  std::vector<AddressPtr> outputs_addr_;
};
using KernelModPtr = std::shared_ptr<KernelMod>;

template <typename T>
inline T *GetDeviceAddress(const std::vector<AddressPtr> &addr_list, size_t index) {
  if (index >= addr_list.size()) {
    MS_LOG(ERROR) << "Address index(" << index << ") out of range(" << addr_list.size() << ")";
    return nullptr;
  }

  if (addr_list[index] == nullptr) {
    MS_LOG(ERROR) << "The device address is nullptr, address index: " << index << ", and the length of 'addr_list' is "
                  << addr_list.size();
    return nullptr;
  }

  if (addr_list[index]->addr == nullptr) {
    MS_LOG(ERROR) << "The memory of device address is nullptr, address index: " << index
                  << ", and the length of 'addr_list' is " << addr_list.size();
    return nullptr;
  }

  if (addr_list[index]->size == 0) {
    MS_LOG(ERROR) << "The size of device address is zero, address index: " << index
                  << ", and the length of 'addr_list' is " << addr_list.size();
    return nullptr;
  }

  return reinterpret_cast<T *>(addr_list[index]->addr);
}
}  // namespace kernel
}  // namespace mindspore

#endif  // MINDSPORE_CCSRC_BACKEND_KERNEL_COMPILER_KERNEL_H_
