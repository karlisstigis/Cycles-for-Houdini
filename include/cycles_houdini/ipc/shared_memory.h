// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <cstdint>
#include <memory>
#include <string>

namespace cycles_houdini::ipc {
inline constexpr std::uint64_t maximum_packet = 8ull * 1024 * 1024 * 1024;
// Houdini reuses one input mapping per renderer process for names with this
// prefix (B1). It rewrites it only after the renderer has replied to the
// request that used it. The renderer keeps its view between requests and
// drops it on command 14 (release input), which Houdini sends before it frees
// or replaces the mapping, so the old section is gone before a new one exists.
inline constexpr std::uint16_t release_input_command = 14;
inline constexpr const char *reused_input_prefix = "Local\\CyclesHoudiniInputR-";
// Owns one named mapping and its mapped view. Destruction releases both, including
// when opening the view fails. Output owners stay alive until the peer ACKs.
class SharedMemory final {
public:
  SharedMemory();
  ~SharedMemory();
  SharedMemory(const SharedMemory &) = delete;
  SharedMemory &operator=(const SharedMemory &) = delete;
  void open(const std::string &name, std::uint64_t size);
  // Maps a whole existing mapping read-only (the reused input); returns its mapped size.
  std::uint64_t open_whole(const std::string &name);
  void create(const std::string &name, std::uint64_t size);
  void *data() const noexcept;

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
} // namespace cycles_houdini::ipc
