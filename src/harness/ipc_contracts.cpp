// SPDX-License-Identifier: Apache-2.0
#include "cycles_houdini/ipc/control_pipe.h"
#include "cycles_houdini/ipc/shared_memory.h"
#include <array>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <chrono>
using namespace cycles_houdini::ipc;
namespace {
void require(bool value) {
  if (!value)
    throw std::runtime_error("IPC contract failed");
}
template <class F> void rejects(F &&f) {
  bool rejected = false;
  try {
    f();
  } catch (const std::exception &) {
    rejected = true;
  }
  require(rejected);
}
struct File {
  FILE *value{std::tmpfile()};
  File() { require(value); }
  ~File() { std::fclose(value); }
};
void wire_contracts() {
  File file;
  Message input{0x8002, 0x0807060504030201ull, 32, "Local\\mapping"};
  write_message(file.value, input);
  std::rewind(file.value);
  std::array<unsigned char, 28> header{};
  require(std::fread(header.data(), 1, header.size(), file.value) == header.size());
  // Golden bytes test independent of the decoder: do not change CHWP v1.
  const std::array<unsigned char, 28> golden{'C', 'H', 'W', 'P', 1, 0, 2, 128, 1, 2, 3,  4, 5, 6,
                                             7,   8,   32,  0,   0, 0, 0, 0,   0, 0, 13, 0, 0, 0};
  require(header == golden);
  std::rewind(file.value);
  Message output;
  require(read_message(file.value, output));
  require(output.command == input.command && output.request == input.request && output.size == input.size &&
          output.mapping == input.mapping);
  require(!read_message(file.value, output));
  rejects([&] { write_message(file.value, {2, 1, 1, {}}); });
  rejects([&] { write_message(file.value, {2, 1, maximum_packet + 1, "map"}); });
  rejects([&] { write_message(file.value, {2, 1, 1, std::string(257, 'a')}); });
  rejects([&] { write_message(file.value, {2, 1, 1, std::string("a\0b", 3)}); });
  for (std::size_t n = 1; n < golden.size(); ++n) {
    File truncated;
    std::fwrite(golden.data(), 1, n, truncated.value);
    std::rewind(truncated.value);
    rejects([&] { read_message(truncated.value, output); });
  }
  File missing_name;
  std::fwrite(golden.data(), 1, golden.size(), missing_name.value);
  std::rewind(missing_name.value);
  rejects([&] { read_message(missing_name.value, output); });
  File ack;
  write_message(ack.value, {acknowledgement_command, 42, 0, {}});
  std::rewind(ack.value);
  require(read_message(ack.value, output) && output.command == 0x7fff && output.request == 42 && !output.size &&
          output.mapping.empty());
}
void mapping_contracts() {
  const auto name =
      "Local\\CH-IpcContract-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count());
  SharedMemory writer;
  writer.create(name, 32);
  std::memcpy(writer.data(), "wire-compatible", 16);
  {
    SharedMemory reader;
    reader.open(name, 32);
    require(std::memcmp(reader.data(), "wire-compatible", 16) == 0);
    // A failed reopen preserves the existing view and releases candidates.
    rejects([&] { reader.open(name + "-missing", 32); });
    require(std::memcmp(reader.data(), "wire-compatible", 16) == 0);
  }
  SharedMemory duplicate;
  rejects([&] { duplicate.create(name, 32); });
  rejects([&] { duplicate.create(name, 0); });
  rejects([&] { duplicate.open(name, maximum_packet + 1); });
}
} // namespace
int main() {
  try {
    wire_contracts();
    mapping_contracts();
    std::cout << "IPC contracts passed\n";
    return 0;
  } catch (const std::exception &e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
