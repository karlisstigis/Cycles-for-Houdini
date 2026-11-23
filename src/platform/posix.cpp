// SPDX-License-Identifier: Apache-2.0
#include "platform.h"
#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <fcntl.h>
#include <stdexcept>
#include <system_error>
#include <unistd.h>
namespace cycles_houdini::platform {
bool reserve_file(const std::filesystem::path &candidate) {
  const auto file = ::open(candidate.c_str(), O_CREAT | O_EXCL | O_WRONLY, 0666);
  if (file >= 0) {
    ::close(file);
    return true;
  }
  if (errno == EEXIST)
    return false;
  throw std::system_error(errno, std::generic_category(), "Cannot reserve output file");
}
bool publish_file(const std::filesystem::path &temporary, const std::filesystem::path &destination,
                  bool replace_existing) {
  if (replace_existing)
    std::filesystem::rename(temporary, destination);
  else {
    if (::link(temporary.c_str(), destination.c_str()) != 0) {
      if (errno == EEXIST)
        return false;
      throw std::system_error(errno, std::generic_category(), "Cannot publish output file");
    }
    std::filesystem::remove(temporary);
  }
  return true;
}
std::string process_memory_json() {
  return "{\"schema_version\":1,\"unavailable\":\"Windows process memory telemetry is unsupported on this platform\"}";
}
std::uint32_t process_id() noexcept { return static_cast<std::uint32_t>(::getpid()); }
std::uint64_t uptime_milliseconds() noexcept {
  return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now().time_since_epoch())
      .count();
}
bool environment_flag(const char *name) {
  const auto *value = std::getenv(name);
  return value && value[0] == '1' && value[1] == '\0';
}
void watch_parent_process(std::uint32_t) {
  throw std::runtime_error("Worker parent observation is unsupported on this platform");
}
} // namespace cycles_houdini::platform
