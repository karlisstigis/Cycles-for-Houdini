// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <cstdint>
#include <filesystem>
#include <string>
namespace cycles_houdini::platform {
// POSIX supports exclusive file reservation, same-volume publication, PID, a
// monotonic clock and environment flags. Process memory reports unavailable
// there. Parent-process observation throws when requested on POSIX.
// false means an existing name; every other OS failure throws system_error.
bool reserve_file(const std::filesystem::path &);
// Same-volume atomic publication. A no-replace collision preserves both files.
// Win32 uses write-through and never falls back to copy/delete across volumes.
bool publish_file(const std::filesystem::path &temporary, const std::filesystem::path &destination,
                  bool replace_existing);
std::string process_memory_json();
std::uint32_t process_id() noexcept;
std::uint64_t uptime_milliseconds() noexcept;
bool environment_flag(const char *name);
// Detached watchdog exits the owned worker when its launching process exits.
void watch_parent_process(std::uint32_t pid);
} // namespace cycles_houdini::platform
