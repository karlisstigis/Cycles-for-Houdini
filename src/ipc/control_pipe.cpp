// SPDX-License-Identifier: Apache-2.0
#include "cycles_houdini/ipc/control_pipe.h"
#include "cycles_houdini/ipc/shared_memory.h"
#include <array>
#include <cstring>
#include <stdexcept>

namespace cycles_houdini::ipc {
namespace {
using Header = std::array<unsigned char, 28>;
template <class T> void put(Header &bytes, std::size_t offset, T value) {
  for (std::size_t i = 0; i < sizeof(T); ++i)
    bytes[offset + i] = static_cast<unsigned char>(value >> (8 * i));
}
template <class T> T get(const Header &bytes, std::size_t offset) {
  T value{};
  for (std::size_t i = 0; i < sizeof(T); ++i)
    value |= T(bytes[offset + i]) << (8 * i);
  return value;
}
void validate(const Message &message) {
  if (message.mapping.size() > 256 || message.size > maximum_packet || (message.size && message.mapping.empty()))
    throw std::invalid_argument("Renderer pipe payload exceeds bounds");
  if (message.mapping.find('\0') != std::string::npos)
    throw std::invalid_argument("Invalid renderer mapping name");
}
} // namespace
bool read_message(FILE *input, Message &message) {
  Header bytes{};
  const auto n = std::fread(bytes.data(), 1, bytes.size(), input);
  if (n == 0 && std::feof(input))
    return false;
  if (n != bytes.size() || std::memcmp(bytes.data(), "CHWP", 4) != 0 ||
      get<std::uint16_t>(bytes, 4) != protocol_version)
    throw std::invalid_argument("Invalid renderer pipe header");
  Message candidate{get<std::uint16_t>(bytes, 6), get<std::uint64_t>(bytes, 8), get<std::uint64_t>(bytes, 16), {}};
  const auto name_length = get<std::uint32_t>(bytes, 24);
  if (name_length > 256 || candidate.size > maximum_packet || (candidate.size && !name_length))
    throw std::invalid_argument("Renderer pipe payload exceeds bounds");
  candidate.mapping.resize(name_length);
  if (name_length && std::fread(candidate.mapping.data(), 1, name_length, input) != name_length)
    throw std::runtime_error("Truncated renderer mapping name");
  validate(candidate);
  message = std::move(candidate);
  return true;
}
void write_message(FILE *output, const Message &message) {
  validate(message);
  Header bytes{};
  std::memcpy(bytes.data(), "CHWP", 4);
  put(bytes, 4, protocol_version);
  put(bytes, 6, message.command);
  put(bytes, 8, message.request);
  put(bytes, 16, message.size);
  put(bytes, 24, std::uint32_t(message.mapping.size()));
  if (!output || std::fwrite(bytes.data(), 1, bytes.size(), output) != bytes.size() ||
      std::fwrite(message.mapping.data(), 1, message.mapping.size(), output) != message.mapping.size() ||
      std::fflush(output) != 0)
    throw std::runtime_error("Renderer pipe disconnected");
}
} // namespace cycles_houdini::ipc
