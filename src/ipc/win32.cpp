// SPDX-License-Identifier: Apache-2.0
// This is the only platform backend for the worker transport.
#include "cycles_houdini/ipc/control_pipe.h"
#include "cycles_houdini/ipc/shared_memory.h"
#include <Windows.h>
#include <io.h>
#include <fcntl.h>
#include <stdexcept>

namespace cycles_houdini::ipc {
struct SharedMemory::Impl {
  HANDLE handle{};
  void *address{};
  ~Impl() {
    if (address)
      UnmapViewOfFile(address);
    if (handle)
      CloseHandle(handle);
  }
};
SharedMemory::SharedMemory() : impl_(std::make_unique<Impl>()) {}
SharedMemory::~SharedMemory() = default;
void *SharedMemory::data() const noexcept { return impl_->address; }
void SharedMemory::open(const std::string &name, std::uint64_t size) {
  if (!size || size > maximum_packet)
    throw std::invalid_argument("Invalid input mapping size");
  auto candidate = std::make_unique<Impl>();
  candidate->handle = OpenFileMappingA(FILE_MAP_READ, FALSE, name.c_str());
  if (!candidate->handle)
    throw std::runtime_error("Could not open input shared memory");
  candidate->address = MapViewOfFile(candidate->handle, FILE_MAP_READ, 0, 0, std::size_t(size));
  if (!candidate->address)
    throw std::runtime_error("Could not map input shared memory");
  impl_ = std::move(candidate);
}
std::uint64_t SharedMemory::open_whole(const std::string &name) {
  auto candidate = std::make_unique<Impl>();
  candidate->handle = OpenFileMappingA(FILE_MAP_READ, FALSE, name.c_str());
  if (!candidate->handle)
    throw std::runtime_error("Could not open input shared memory");
  candidate->address = MapViewOfFile(candidate->handle, FILE_MAP_READ, 0, 0, 0);
  MEMORY_BASIC_INFORMATION info{};
  if (!candidate->address || !VirtualQuery(candidate->address, &info, sizeof(info)))
    throw std::runtime_error("Could not map input shared memory");
  impl_ = std::move(candidate);
  return std::uint64_t(info.RegionSize);
}
void SharedMemory::create(const std::string &name, std::uint64_t size) {
  if (!size || size > maximum_packet)
    throw std::invalid_argument("Invalid output mapping size");
  auto candidate = std::make_unique<Impl>();
  candidate->handle =
      CreateFileMappingA(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, DWORD(size >> 32), DWORD(size), name.c_str());
  if (!candidate->handle || GetLastError() == ERROR_ALREADY_EXISTS)
    throw std::runtime_error("Could not create unique output shared memory");
  candidate->address = MapViewOfFile(candidate->handle, FILE_MAP_WRITE, 0, 0, std::size_t(size));
  if (!candidate->address)
    throw std::runtime_error("Could not map output shared memory");
  impl_ = std::move(candidate);
}
struct WorkerControlPipe::Impl {
  FILE *output{};
  ~Impl() {
    if (output)
      std::fclose(output);
  }
};
WorkerControlPipe::WorkerControlPipe() : impl_(std::make_unique<Impl>()) {
  if (_setmode(_fileno(stdin), _O_BINARY) < 0)
    throw std::runtime_error("Could not configure renderer input pipe");
  const int descriptor = _dup(_fileno(stdout));
  if (descriptor < 0)
    throw std::runtime_error("Could not duplicate renderer control pipe");
  if (_setmode(descriptor, _O_BINARY) < 0) {
    _close(descriptor);
    throw std::runtime_error("Could not configure renderer control pipe");
  }
  impl_->output = _fdopen(descriptor, "wb");
  if (!impl_->output) {
    _close(descriptor);
    throw std::runtime_error("Could not open renderer control stream");
  }
  if (_dup2(_fileno(stderr), _fileno(stdout)) < 0)
    throw std::runtime_error("Could not redirect renderer library stdout");
  const auto error_handle = reinterpret_cast<HANDLE>(_get_osfhandle(_fileno(stderr)));
  if (error_handle == INVALID_HANDLE_VALUE || !SetStdHandle(STD_OUTPUT_HANDLE, error_handle))
    throw std::runtime_error("Could not redirect renderer Win32 stdout");
}
WorkerControlPipe::~WorkerControlPipe() = default;
bool WorkerControlPipe::read(Message &message) { return read_message(stdin, message); }
void WorkerControlPipe::send(const Message &message) { write_message(impl_->output, message); }
void WorkerControlPipe::write_smoke_noise() {
  std::printf("CH_WORKER_CRT_STDOUT_NOISE\n");
  std::fflush(stdout);
  const char noise[] = "CH_WORKER_WIN32_STDOUT_NOISE\n";
  DWORD written{};
  if (!WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), noise, DWORD(sizeof(noise) - 1), &written, nullptr) ||
      written != sizeof(noise) - 1)
    throw std::runtime_error("Could not write transport smoke noise");
}
} // namespace cycles_houdini::ipc
