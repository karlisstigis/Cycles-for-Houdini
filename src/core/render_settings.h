// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
namespace ccl {
class Session;
class SessionParams;
} // namespace ccl

namespace cycles_houdini {
void validate_render_settings(const RenderSettings &settings);
void configure_render_settings(const RenderSettings &settings, ccl::Session &session, ccl::SessionParams &params,
                               bool interactive);
} // namespace cycles_houdini
