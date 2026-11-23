// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <cstdint>
#include <cstdio>
#include <memory>
#include <string>

namespace cycles_houdini::ipc {
inline constexpr std::uint16_t protocol_version = 1;
inline constexpr std::uint16_t acknowledgement_command = 0x7fff;
struct Message {
  std::uint16_t command{};
  std::uint64_t request{}, size{};
  std::string mapping;
};
// CHWP v1: a 28-byte little-endian header followed by the mapping name, without
// a terminator. FILE streams are borrowed and must already be in binary mode.
// A clean EOF returns false; truncated or invalid input throws before dispatch.
bool read_message(FILE *input, Message &message);
void write_message(FILE *output, const Message &message);

// Owns a private duplicate of inherited stdout and redirects library stdout to
// stderr (CRT and OS handles). Only this stream may carry the binary protocol.
class WorkerControlPipe final {
public:
  WorkerControlPipe();
  ~WorkerControlPipe();
  WorkerControlPipe(const WorkerControlPipe &) = delete;
  WorkerControlPipe &operator=(const WorkerControlPipe &) = delete;
  bool read(Message &message);
  void send(const Message &message);
  static void write_smoke_noise();

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
} // namespace cycles_houdini::ipc
