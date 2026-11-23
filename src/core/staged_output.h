// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <filesystem>
#include <string>

namespace cycles_houdini {
// Write beside the destination, then replace only after the encoder has closed.
// Scope exit removes only this operation's uniquely reserved temporary file.
class StagedOutput {
public:
  // The suffix of the temporary name selects the encoder (OIIO picks it by extension).
  explicit StagedOutput(const std::string &destination, const std::string &suffix = ".exr");
  ~StagedOutput();
  StagedOutput(const StagedOutput &) = delete;
  StagedOutput &operator=(const StagedOutput &) = delete;
  std::string temporary_filename() const;
  bool commit(bool replace_existing = true);

private:
  std::filesystem::path destination_, temporary_;
};
} // namespace cycles_houdini
