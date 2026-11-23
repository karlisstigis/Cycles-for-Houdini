// SPDX-License-Identifier: Apache-2.0
#include "../platform/platform.h"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <system_error>
namespace fs = std::filesystem;
namespace os = cycles_houdini::platform;
namespace {
void require(bool value) {
  if (!value)
    throw std::runtime_error("Platform contract failed");
}
std::string read(const fs::path &path) {
  std::ifstream file(path);
  return {std::istreambuf_iterator<char>(file), {}};
}
void write(const fs::path &path, const char *value) {
  std::ofstream file(path);
  file << value;
  require(bool(file));
}
struct Directory {
  fs::path path;
  explicit Directory(const fs::path &root)
      : path(root / ("CH-PlatformContract-" + std::to_string(os::process_id()) + "-" +
                     std::to_string(os::uptime_milliseconds()))) {
    require(fs::create_directory(path));
  }
  ~Directory() {
    std::error_code error;
    // Only this contract's known files and then its empty directory.
    for (const auto &name : {"first", "second", "third", "target"})
      fs::remove(path / name, error);
    fs::remove(path, error);
  }
};
} // namespace
int main(int argc, char **argv) {
  try {
    require(os::process_id() > 0);
    const auto started = os::uptime_milliseconds();
    const auto memory = os::process_memory_json();
    require(memory.find("\"schema_version\":1") != std::string::npos);
    require(memory.find("\"pid\":" + std::to_string(os::process_id())) != std::string::npos ||
            memory.find("\"unavailable\"") != std::string::npos);
    require(os::uptime_milliseconds() >= started);
    Directory directory(argc > 1 ? fs::u8path(argv[1]) : fs::current_path());
    const auto first = directory.path / "first", second = directory.path / "second";
    const auto third = directory.path / "third", destination = directory.path / "target";
    require(os::reserve_file(first));
    write(first, "old");
    require(!os::reserve_file(first) && read(first) == "old");
    require(os::publish_file(first, destination, false));
    require(!fs::exists(first) && read(destination) == "old");
    require(os::reserve_file(second));
    write(second, "new");
    require(!os::publish_file(second, destination, false));
    require(read(second) == "new" && read(destination) == "old");
    require(os::publish_file(second, destination, true));
    require(!fs::exists(second) && read(destination) == "new");
    require(os::reserve_file(third));
    bool rejected = false;
    try {
      os::publish_file(third, directory.path / "missing-parent/target", false);
    } catch (const std::system_error &) {
      rejected = true;
    }
    require(rejected && fs::exists(third) && read(destination) == "new");
    rejected = false;
    try {
      os::reserve_file(directory.path / "missing-parent/file");
    } catch (const std::system_error &) {
      rejected = true;
    }
    require(rejected);
    std::cout << "Platform memory/process/atomic publication contracts passed\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
