// SPDX-License-Identifier: Apache-2.0
// Development-only debugger for the exact child process launched by run-gui-test.ps1.
#define WIN32_LEAN_AND_MEAN
#include <Windows.h>
#include <DbgHelp.h>
#include <chrono>
#include <fstream>
#include <iostream>
#include <string>
#include <array>
#include <vector>
#include <thread>

int sample_ui(int argc, char **argv) {
  if (argc != 6)
    return 2;
  const auto pid = static_cast<DWORD>(std::stoul(argv[2])), tid = static_cast<DWORD>(std::stoul(argv[3]));
  std::ofstream log(argv[4]);
  HANDLE process = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, FALSE, pid);
  HANDLE thread = OpenThread(THREAD_GET_CONTEXT | THREAD_SUSPEND_RESUME | THREAD_QUERY_INFORMATION, FALSE, tid);
  if (!process || !thread) {
    log << "Open failed " << GetLastError() << '\n';
    if (process)
      CloseHandle(process);
    if (thread)
      CloseHandle(thread);
    return 1;
  }
  SymSetOptions(SYMOPT_UNDNAME | SYMOPT_DEFERRED_LOADS | SYMOPT_FAIL_CRITICAL_ERRORS);
  if (!SymInitialize(process, nullptr, TRUE)) {
    CloseHandle(process);
    CloseHandle(thread);
    return 1;
  }
  struct Sample {
    double at{}, paused{};
    std::array<DWORD64, 40> addresses{};
    int count{};
  };
  std::vector<Sample> samples;
  const auto start = std::chrono::steady_clock::now();
  const auto duration = std::chrono::milliseconds(std::stoul(argv[5]));
  while (std::chrono::steady_clock::now() - start < duration) {
    Sample sample;
    sample.at = std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
    const auto began = std::chrono::steady_clock::now();
    if (SuspendThread(thread) == DWORD(-1))
      break;
    CONTEXT context{};
    context.ContextFlags = CONTEXT_FULL;
    if (GetThreadContext(thread, &context)) {
      STACKFRAME64 frame{};
      frame.AddrPC = {context.Rip, 0, AddrModeFlat};
      frame.AddrStack = {context.Rsp, 0, AddrModeFlat};
      frame.AddrFrame = {context.Rbp, 0, AddrModeFlat};
      while (sample.count < int(sample.addresses.size()) && frame.AddrPC.Offset) {
        sample.addresses[sample.count++] = frame.AddrPC.Offset;
        if (!StackWalk64(IMAGE_FILE_MACHINE_AMD64, process, thread, &frame, &context, nullptr, SymFunctionTableAccess64,
                         SymGetModuleBase64, nullptr))
          break;
      }
    }
    ResumeThread(thread);
    sample.paused = std::chrono::duration<double>(std::chrono::steady_clock::now() - began).count();
    samples.push_back(sample);
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  }
  // Symbol formatting and allocations stay outside the suspended interval.
  for (const auto &sample : samples) {
    log << "Sample at=" << sample.at << " paused_seconds=" << sample.paused << '\n';
    for (int depth = 0; depth < sample.count; ++depth) {
      const auto address = sample.addresses[depth];
      IMAGEHLP_MODULE64 module{};
      module.SizeOfStruct = sizeof(module);
      SymGetModuleInfo64(process, address, &module);
      alignas(SYMBOL_INFO) char storage[sizeof(SYMBOL_INFO) + 1024]{};
      auto *symbol = reinterpret_cast<SYMBOL_INFO *>(storage);
      symbol->SizeOfStruct = sizeof(SYMBOL_INFO);
      symbol->MaxNameLen = 1023;
      DWORD64 displacement = 0;
      const bool named = SymFromAddr(process, address, &displacement, symbol);
      log << "  " << depth << " " << module.ModuleName << "+0x" << std::hex << (address - module.BaseOfImage) << " "
          << (named ? symbol->Name : "") << " +0x" << displacement << std::dec << '\n';
    }
  }
  SymCleanup(process);
  CloseHandle(process);
  CloseHandle(thread);
  return samples.empty() ? 1 : 0;
}

int main(int argc, char **argv) {
  if (argc > 1 && std::string(argv[1]) == "--sample")
    return sample_ui(argc, argv);
  if (argc != 3) {
    std::cerr << "Usage: ch_gui_debugger OWN_TEST_PID LOG_PATH\n";
    return 2;
  }
  const auto pid = static_cast<DWORD>(std::stoul(argv[1]));
  std::ofstream log(argv[2]);
  if (!DebugActiveProcess(pid)) {
    log << "Attach failed: " << GetLastError() << '\n';
    return 1;
  }
  DebugSetProcessKillOnExit(FALSE);
  HANDLE process = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, FALSE, pid);
  SymSetOptions(SYMOPT_UNDNAME | SYMOPT_DEFERRED_LOADS | SYMOPT_FAIL_CRITICAL_ERRORS);
  SymInitialize(process, nullptr, TRUE);
  bool done = false;
  const auto start = std::chrono::steady_clock::now();
  while (!done && std::chrono::steady_clock::now() - start < std::chrono::seconds(150)) {
    DEBUG_EVENT event{};
    if (!WaitForDebugEvent(&event, 1000))
      continue;
    DWORD disposition = DBG_CONTINUE;
    switch (event.dwDebugEventCode) {
    case EXCEPTION_DEBUG_EVENT: {
      const auto &exception = event.u.Exception;
      const auto code = exception.ExceptionRecord.ExceptionCode;
      if (code != EXCEPTION_BREAKPOINT)
        disposition = DBG_EXCEPTION_NOT_HANDLED;
      if (code == EXCEPTION_ACCESS_VIOLATION || !exception.dwFirstChance) {
        log << "Exception 0x" << std::hex << code << " at " << exception.ExceptionRecord.ExceptionAddress
            << " first_chance=" << exception.dwFirstChance << " thread=" << std::dec << event.dwThreadId << '\n';
        HANDLE thread = OpenThread(THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, FALSE, event.dwThreadId);
        CONTEXT context{};
        context.ContextFlags = CONTEXT_FULL;
        if (thread && GetThreadContext(thread, &context)) {
          STACKFRAME64 frame{};
          frame.AddrPC = {context.Rip, 0, AddrModeFlat};
          frame.AddrStack = {context.Rsp, 0, AddrModeFlat};
          frame.AddrFrame = {context.Rbp, 0, AddrModeFlat};
          for (int depth = 0; depth < 48 && frame.AddrPC.Offset; ++depth) {
            const auto address = frame.AddrPC.Offset;
            IMAGEHLP_MODULE64 module{};
            module.SizeOfStruct = sizeof(module);
            SymGetModuleInfo64(process, address, &module);
            alignas(SYMBOL_INFO) char storage[sizeof(SYMBOL_INFO) + 1024]{};
            auto *symbol = reinterpret_cast<SYMBOL_INFO *>(storage);
            symbol->SizeOfStruct = sizeof(SYMBOL_INFO);
            symbol->MaxNameLen = 1023;
            DWORD64 displacement = 0;
            const bool named = SymFromAddr(process, address, &displacement, symbol);
            log << "  " << depth << " " << module.ModuleName << "+0x" << std::hex << (address - module.BaseOfImage)
                << " " << (named ? symbol->Name : "") << " +0x" << displacement << std::dec << '\n';
            if (!StackWalk64(IMAGE_FILE_MACHINE_AMD64, process, thread, &frame, &context, nullptr,
                             SymFunctionTableAccess64, SymGetModuleBase64, nullptr))
              break;
          }
        }
        if (thread)
          CloseHandle(thread);
        log.flush();
      }
      break;
    }
    case LOAD_DLL_DEBUG_EVENT:
      SymLoadModuleEx(process, event.u.LoadDll.hFile, nullptr, nullptr,
                      reinterpret_cast<DWORD64>(event.u.LoadDll.lpBaseOfDll), 0, nullptr, 0);
      if (event.u.LoadDll.hFile)
        CloseHandle(event.u.LoadDll.hFile);
      break;
    case CREATE_PROCESS_DEBUG_EVENT:
      if (event.u.CreateProcessInfo.hFile)
        CloseHandle(event.u.CreateProcessInfo.hFile);
      CloseHandle(event.u.CreateProcessInfo.hProcess);
      CloseHandle(event.u.CreateProcessInfo.hThread);
      break;
    case CREATE_THREAD_DEBUG_EVENT:
      CloseHandle(event.u.CreateThread.hThread);
      break;
    case EXIT_PROCESS_DEBUG_EVENT:
      log << "Process exit=" << event.u.ExitProcess.dwExitCode << '\n';
      done = true;
      break;
    default:
      break;
    }
    ContinueDebugEvent(event.dwProcessId, event.dwThreadId, disposition);
  }
  SymCleanup(process);
  CloseHandle(process);
  if (!done)
    DebugActiveProcessStop(pid);
  return 0;
}
