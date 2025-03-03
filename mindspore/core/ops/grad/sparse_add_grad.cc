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

#include "ops/grad/sparse_add_grad.h"
#include <set>
#include <map>
#include <memory>
#include <string>
#include <vector>
#include "ops/op_utils.h"
#include "utils/check_convert_utils.h"
#include "abstract/ops/primitive_infer_map.h"
#include "mindapi/src/helper.h"

namespace mindspore {
namespace ops {
using mindspore::abstract::AbstractTensor;
using mindspore::abstract::AbstractTuple;

namespace {
constexpr size_t kSparseAddGradIndex0 = 0;
constexpr size_t kSparseAddGradIndex1 = 1;
constexpr size_t kSparseAddGradIndex2 = 2;

mindspore::TypePtr SparseAddGradInferType(const std::string &op_name, const AbstractBasePtrList &args_spec_list,
                                          size_t index) {
  auto tensor = mindspore::abstract::CheckArg<AbstractTensor>(op_name, args_spec_list, index);
  return tensor->element()->BuildType();
}
}  // namespace

void SparseAddGrad::Init() {}

AbstractBasePtr SparseAddGradInfer(const abstract::AnalysisEnginePtr &, const PrimitivePtr &primitive,
                                   const std::vector<AbstractBasePtr> &input_args) {
  MS_EXCEPTION_IF_NULL(primitive);
  auto name = primitive->name();
  const size_t kInputNum = 4;
  (void)CheckAndConvertUtils::CheckInputArgs(input_args, kEqual, kInputNum, name);
  auto type = SparseAddGradInferType(name, input_args, 0);

  std::shared_ptr<AbstractTensor> dx1 = nullptr;
  std::shared_ptr<AbstractTensor> dx2 = nullptr;
  auto x1_shape = CheckAndConvertUtils::ConvertShapePtrToShapeMap(input_args[kSparseAddGradIndex1]->BuildShape());
  bool x1_is_dyn_shape = !x1_shape[kMaxShape].empty();
  auto dx1_shape = x1_shape[kShape];
  ShapeVector shp = {dx1_shape.at(0)};
  if (x1_is_dyn_shape) {
    auto dx1_min_shape = x1_shape[kMinShape];
    auto dx1_max_shape = x1_shape[kMaxShape];
    ShapeVector min_shp = {dx1_min_shape.at(0)};
    ShapeVector max_shp = {dx1_max_shape.at(0)};
    dx1 = std::make_shared<AbstractTensor>(type, std::make_shared<mindspore::abstract::Shape>(shp, min_shp, max_shp));
  } else {
    dx1 = std::make_shared<AbstractTensor>(type, std::make_shared<mindspore::abstract::Shape>(shp));
  }

  auto x2_shape = CheckAndConvertUtils::ConvertShapePtrToShapeMap(input_args[kSparseAddGradIndex2]->BuildShape());
  bool x2_is_dyn_shape = !x2_shape[kMaxShape].empty();
  ShapeVector dx2_shape = x2_shape[kShape];
  shp = {dx2_shape.at(0)};
  if (x2_is_dyn_shape) {
    auto dx2_min_shape = x2_shape[kMinShape];
    auto dx2_max_shape = x2_shape[kMaxShape];
    ShapeVector min_shp = {dx2_min_shape.at(0)};
    ShapeVector max_shp = {dx2_max_shape.at(0)};
    dx2 = std::make_shared<AbstractTensor>(type, std::make_shared<mindspore::abstract::Shape>(shp, min_shp, max_shp));
  } else {
    dx2 = std::make_shared<AbstractTensor>(type, std::make_shared<mindspore::abstract::Shape>(shp));
  }
  AbstractBasePtrList ret = {dx1, dx2};
  return std::make_shared<AbstractTuple>(ret);
}

MIND_API_OPERATOR_IMPL(SparseAddGrad, BaseOperator);
REGISTER_PRIMITIVE_EVAL_IMPL(SparseAddGrad, prim::kPrimSparseAddGrad, SparseAddGradInfer, nullptr, true);
}  // namespace ops
}  // namespace mindspore
