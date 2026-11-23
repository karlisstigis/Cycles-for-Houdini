// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <atomic>
#include <chrono>
#include <cstdint>

namespace cycles_houdini {
// A cooperative host continuation, never a source diagnostic or engine failure.
struct NativePreparationPending {};
struct NativePreparationBudget {
  using Clock = std::chrono::steady_clock;
  NativePreparationBudget() : capture(++next_capture_) {}
  void begin_turn() { deadline = Clock::now() + std::chrono::milliseconds(20); }
  bool expired() const { return Clock::now() >= deadline; }
  const std::uint64_t capture;
  std::uint64_t turns{};

private:
  inline static std::atomic<std::uint64_t> next_capture_{};
  Clock::time_point deadline{};
};
} // namespace cycles_houdini
