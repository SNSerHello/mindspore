/**
 * Copyright 2021 Huawei Technologies Co., Ltd
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

#include "profiler/device/profiling.h"

#include <cxxabi.h>
#include <cmath>
#include <ctime>
#include "include/common/pybind_api/api_register.h"
#include "utils/log_adapter.h"
#include "include/common/utils/utils.h"
#include "utils/ms_context.h"

namespace mindspore {
namespace profiler {
std::shared_ptr<Profiler> Profiler::GetInstance(const std::string &name) noexcept {
  if (auto iter = instance_map_.find(name); iter != instance_map_.end()) {
    return iter->second;
  }

  MS_LOG(WARNING) << "Profiler instance " << name << " not found.";
  return nullptr;
}

bool Profiler::Register(const std::string &name, const std::shared_ptr<Profiler> &instance) {
  if (instance_map_.find(name) != instance_map_.end()) {
    MS_LOG(WARNING) << name << " has been registered.";
  } else {
    (void)instance_map_.emplace(name, instance);
  }

  return true;
}

uint64_t Profiler::GetHostMonoTimeStamp() const {
  struct timespec ts;
#if defined(_WIN32) || defined(_WIN64)
  if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
    MS_LOG(ERROR) << "Get host timestamp failed";
    return 0;
  }
#else
  if (clock_gettime(CLOCK_MONOTONIC_RAW, &ts) != 0) {
    MS_LOG(ERROR) << "Get host timestamp failed";
    return 0;
  }
#endif
  constexpr uint64_t kNSecondInSecond = 1000000000;
  uint64_t cur_time_stamp = ts.tv_sec * kNSecondInSecond + ts.tv_nsec;
  return cur_time_stamp;
}

uint64_t Profiler::GetRealTimeStamp() const {
  struct timeval tv = {0, 0};
  (void)gettimeofday(&tv, NULL);
  int64_t kUSecondInSecond = 1000000;
  int64_t ts = kUSecondInSecond * static_cast<int64_t>(tv.tv_sec);
  ts += static_cast<int64_t>(tv.tv_usec);
  // us timestamp
  return (uint64_t)ts;
}

void Profiler::SetRunTimeData(const std::string &op_name, const float time_elapsed) {
  std::shared_lock<std::shared_mutex> lock(op_map_mutex_);
  auto iter = op_info_map_.find(op_name);
  if (iter != op_info_map_.end()) {
    iter->second.op_host_cost_time += time_elapsed;
  }
}

void Profiler::SetRunTimeData(const std::string &op_name, const uint64_t start, const float duration) {
  std::shared_lock<std::shared_mutex> lock(op_map_mutex_);
  auto iter = op_info_map_.find(op_name);
  if (iter != op_info_map_.end()) {
    iter->second.start_duration.emplace_back(StartDuration({start, duration}));
  }
}

void Profiler::RecordOneStepStartEndInfo() {
  // Multi-graph dotting data is not supported.
  std::lock_guard<std::mutex> locker(record_mutex_);
  std::string step_end_op_name;
  std::string op_type = "GetNext";
  uint32_t vector_size = (uint32_t)step_start_end_info_vector_.size();
  step_start_end_info_.iter_start_op_name = step_start_end_info_vector_[0];
  step_start_end_info_.fp_start_op_name = step_start_end_info_vector_[0];

  // If is the first step, the step_start_end_info_vector_ length is 1.
  if (vector_size > 1) {
    // Iterate through step_start_end_info_vector_ for the repeat operator, which is the operator of the next step and
    // is preceded by iter_end_op of the current step.
    for (uint32_t i = vector_size - 1; i > 0; i--) {
      step_end_op_name = step_start_end_info_vector_[i];
      uint32_t j = 0;
      for (; j < i; j++) {
        if (step_end_op_name == step_start_end_info_vector_[j]) {
          has_find = true;
          iter_end_op_index = i - 1;
          break;
        }
      }
      if (i == j) {
        break;
      }
    }
    if (has_find) {
      for (uint32_t i = 0; i < vector_size; i++) {
        std::string op_name = step_start_end_info_vector_[i];
        auto op_type_begin_iter = op_name.rfind('/') + 1;
        auto op_type_end_iter = op_name.rfind('-');
        auto type = op_name.substr(op_type_begin_iter, op_type_end_iter - op_type_begin_iter);
        if (type == op_type) {
          if (i == 0) {
            // If the type of the first operator is GetNext, the next operator of it is the fp_start operator.
            step_start_end_info_.fp_start_op_name = step_start_end_info_vector_[i + 1];
          } else {
            // If the data processing operator is iter_start, the type of the fp_start operator should be GetNext.
            step_start_end_info_.fp_start_op_name = op_name;
          }
          break;
        }
      }
      step_start_end_info_.iter_end_op_name = step_start_end_info_vector_[iter_end_op_index];
      // Delete the operator of the current step.
      (void)step_start_end_info_vector_.erase(step_start_end_info_vector_.begin(),
                                              step_start_end_info_vector_.begin() + iter_end_op_index + 1);
    } else {
      step_start_end_info_.fp_start_op_name = step_start_end_info_vector_[1];
      step_start_end_info_.iter_end_op_name = step_start_end_info_vector_[step_start_end_info_vector_.size() - 1];
      step_start_end_info_vector_.clear();
    }
  } else {
    step_start_end_info_vector_.clear();
  }
  all_step_start_end_info_.push_back(step_start_end_info_);
  step_start_end_info_.iter_start_op_name = "";
  step_start_end_info_.fp_start_op_name = "";
}

void Profiler::RecordOneStepStartEndInfo(const std::string op_name) {
  std::lock_guard<std::mutex> locker(record_mutex_);
  if (step_start_end_info_.iter_start_op_name.empty()) {
    step_start_end_info_.iter_start_op_name = op_name;
    step_start_end_info_.fp_start_op_name = op_name;
  }

  std::string fp_start_op_name = step_start_end_info_.fp_start_op_name;

  auto op_type_begin_iter = fp_start_op_name.rfind('/') + 1;
  auto op_type_end_iter = fp_start_op_name.rfind('-');
  auto op_type = fp_start_op_name.substr(op_type_begin_iter, op_type_end_iter - op_type_begin_iter);
  if (op_type == "InitDataSetQueue" || op_type == "GetNext") {
    step_start_end_info_.fp_start_op_name = op_name;
  }
  step_start_end_info_.iter_end_op_name = op_name;
  step_start_end_info_vector_.push_back(op_name);
}

std::shared_ptr<ProfilerManager> &ProfilerManager::GetInstance() {
  MS_EXCEPTION_IF_NULL(profiler_manager_inst_);
  return profiler_manager_inst_;
}

bool ProfilerManager::GetProfilingEnableFlag() const {
  if (auto gpu_instance = Profiler::GetInstance(kGPUDevice); gpu_instance != nullptr) {
    return gpu_instance->GetEnableFlag();
  }

  if (auto ascend_instance = Profiler::GetInstance(kAscendDevice); ascend_instance != nullptr) {
    return ascend_instance->GetEnableFlag();
  }

  return false;
}

void ProfilerManager::RecordOneStepStartEndInfo() const {
  if (auto gpu_instance = Profiler::GetInstance(kGPUDevice); gpu_instance != nullptr && gpu_instance->GetEnableFlag()) {
    gpu_instance->RecordOneStepStartEndInfo();
  }
}

std::string ProfilerManager::GetProfilingOptions() const {
  if (auto ascend_instance = Profiler::GetInstance(kAscendDevice); ascend_instance != nullptr) {
    return ascend_instance->GetProfilingOptions();
  }

  return "";
}

REGISTER_PYBIND_DEFINE(ProfilerManager_, ([](const py::module *m) {
                         (void)py::class_<ProfilerManager, std::shared_ptr<ProfilerManager>>(*m, "ProfilerManager")
                           .def_static("get_instance", &ProfilerManager::GetInstance, "ProfilerManager get_instance.")
                           .def("dynamic_status", &ProfilerManager::GetNetDynamicShapeStatus, "dynamic_status");
                       }));

REGISTER_PYBIND_DEFINE(Profiler_, ([](const py::module *m) {
                         (void)py::class_<Profiler, std::shared_ptr<Profiler>>(*m, "Profiler")
                           .def_static("get_instance", &Profiler::GetInstance, py::arg("device_name"),
                                       "Profiler get_instance.")
                           .def("init", &Profiler::Init, py::arg("profiling_path"), py::arg("device_id") = py::int_(0),
                                py::arg("profiling_options") = py::str(""), "init")
                           .def("start", &Profiler::Start, "start")
                           .def("stop", &Profiler::Stop, "stop")
                           .def("finalize", &Profiler::Finalize, "finalize")
                           .def("step_profiling_enable", &Profiler::StepProfilingEnable, py::arg("enable_flag"),
                                "enable or disable step profiling");
                       }));
}  // namespace profiler
}  // namespace mindspore
