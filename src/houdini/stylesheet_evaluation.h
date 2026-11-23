// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <memory>
#include <string>
#include <vector>
#include <STY/STY_Styler.h>
#include <STY/STY_Results.h>
class STY_Subject;
class STY_SubjectGroup;

namespace cycles_houdini {
// Host-only native styling. Failed fields are removed from cloned source
// layers before native re-evaluation, preserving lower-priority native rules.
// No STY object or borrowed geometry pointer escapes acquisition.
class NativeStyleEvaluation {
public:
  NativeStyleEvaluation(double time, const std::string &cwd);
  ~NativeStyleEvaluation();
  STY_StyleSheetHandle prepare(const STY_StyleSheetHandle &);
  STY_Styler object(const std::vector<STY_StyleSheetHandle> &, const STY_Subject &);
  std::vector<STY_Styler> evaluate(const std::vector<STY_StyleSheetHandle> &, const STY_SubjectGroup &,
                                   UT_Array<STY_Results> &, const STY_Subject *ancestor = nullptr);
  bool scripted() const;
  std::vector<std::string> resources() const;
  std::string save(const STY_Styler &);

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
inline constexpr const char *stylesheet_recovery_category = "cycles:stylesheet_recovery";
} // namespace cycles_houdini
