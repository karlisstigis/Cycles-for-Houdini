// SPDX-License-Identifier: Apache-2.0
#include "gpu_concurrency_contracts.h"
#include "device/device.h"
#include "device/queue.h"
#include "session/session.h"
#include <iostream>
#include <stdexcept>

namespace cycles_houdini::harness {
namespace {
// Exercise the production queue calculation with controlled memory, without initializing a GPU.
class BudgetDevice final : public ccl::Device {
public:
  BudgetDevice(ccl::Stats &stats, ccl::Profiler &profiler) : Device(ccl::DeviceInfo(), stats, profiler, true) {}
  ccl::BVHLayoutMask get_bvh_layout_mask(uint64_t) const override { return 0; }
  void const_copy_to(const char *, void *, size_t) override {}

protected:
  void mem_alloc(ccl::device_memory &) override {}
  void mem_copy_to(ccl::device_memory &) override {}
  void mem_move_to_host(ccl::device_memory &) override {}
  void mem_copy_from(ccl::device_memory &, size_t, size_t, size_t, size_t) override {}
  void mem_zero(ccl::device_memory &) override {}
  void mem_free(ccl::device_memory &) override {}
};
class BudgetQueue final : public ccl::DeviceQueue {
public:
  static constexpr size_t block = 65536, state_bytes = 256;
  size_t free_blocks = 24;
  explicit BudgetQueue(ccl::Device *device) : DeviceQueue(device) {}
  void init_execution() override {}
  void load_image_info() override {}
  bool enqueue(ccl::DeviceKernel, int, const ccl::DeviceKernelArguments &) override { return false; }
  bool synchronize() override { return true; }
  void zero_to_device(ccl::device_memory &) override {}
  void copy_to_device(ccl::device_memory &) override {}
  void copy_from_device(ccl::device_memory &) override {}
  void *copy_from_device_synchronized(ccl::device_memory &, ccl::vector<uint8_t> &) override { return nullptr; }

protected:
  ccl::ConcurrentStatesParams concurrent_states_params() const override {
    ccl::ConcurrentStatesParams p;
    p.baseline = 4 * block;
    p.min = block;
    p.max = 64 * block;
    return p;
  }
  void get_memory_info(size_t &total, size_t &free) const override {
    total = 40 * block * state_bytes;
    free = free_blocks * block * state_bytes;
  }
};
} // namespace
void gpu_concurrency_contracts(const std::string &mode) {
  int checks = 0;
  const auto require = [&](bool ok, const char *message) {
    ++checks;
    if (!ok)
      throw std::runtime_error(message);
  };
  ccl::SessionParams final_params, interactive_params;
  require(final_params.use_gpu_concurrent_states_growth, "Native session growth default changed");
  interactive_params.use_gpu_concurrent_states_growth = false;
  require(final_params.modified(interactive_params), "Growth policy change did not recreate session");
  ccl::Stats stats;
  ccl::Profiler profiler;
  BudgetDevice interactive(stats, profiler), final(stats, profiler);
  require(interactive.use_gpu_concurrent_states_growth && final.use_gpu_concurrent_states_growth,
          "Native device growth default changed");
  interactive.use_gpu_concurrent_states_growth = false;
  BudgetQueue iq(&interactive), fq(&final);
  const int block = BudgetQueue::block;
  int interactive_high = 4 * block, final_high = 10 * block, low = 2 * block;
  if (mode == "grow-override")
    interactive_high = final_high = 5 * block;
  else if (mode == "bounds-override") {
    interactive_high = 3 * block;
    final_high = 7 * block / 2;
    low = 3 * block / 2;
  } else if (mode != "default")
    throw std::invalid_argument("Unknown GPU concurrency contract mode");
  const size_t low_free = mode == "bounds-override" ? 2 : 6;
  iq.free_blocks = fq.free_blocks = 24;
  require(iq.num_concurrent_states(BudgetQueue::state_bytes) == interactive_high,
          "Interactive concurrency or explicit override changed");
  require(fq.num_concurrent_states(BudgetQueue::state_bytes) == final_high,
          "Final concurrency growth or explicit override changed");
  iq.free_blocks = fq.free_blocks = low_free;
  require(iq.num_concurrent_states(BudgetQueue::state_bytes) == low, "Interactive low-memory shrink changed");
  require(fq.num_concurrent_states(BudgetQueue::state_bytes) == low, "Final low-memory shrink changed");
  iq.free_blocks = 24;
  require(iq.num_concurrent_states(BudgetQueue::state_bytes) == interactive_high,
          "Other device changed interactive policy");
  require(!interactive.use_gpu_concurrent_states_growth && final.use_gpu_concurrent_states_growth,
          "Policy leaked between devices");
  std::cout << "{\"result\":\"passed\",\"mode\":\"" << mode << "\",\"checks\":" << checks << "}\n";
}
} // namespace cycles_houdini::harness
