# SPDX-License-Identifier: Apache-2.0
# NGX uses its header-only dynamic loader. Never stage an external feature DLL.
find_path(DLSS_INCLUDE_DIR nvsdk_ngx.h
  HINTS "${DLSS_SDK_ROOT}/include" "$ENV{DLSS_SDK_ROOT}/include"
  PATHS "${CYCLES_HOUDINI_INTEGRATION_ROOT}/third_party/dlss/include")
if(DLSS_INCLUDE_DIR)
  foreach(header nvsdk_ngx_defs.h nvsdk_ngx_defs_dlssd.h nvsdk_ngx_loader.h nvsdk_ngx_params.h)
    if(NOT EXISTS "${DLSS_INCLUDE_DIR}/${header}")
      message(FATAL_ERROR "Incomplete DLSS SDK: missing ${header}")
    endif()
  endforeach()
  file(READ "${DLSS_INCLUDE_DIR}/nvsdk_ngx.h" CH_NGX_API)
  if(NOT CH_NGX_API MATCHES "NVSDK_NGX_CUDA_Init_with_ProjectID" OR NOT CH_NGX_API MATCHES "NVSDK_NGX_HEADER_ONLY")
    message(FATAL_ERROR "DLSS SDK must support header-only NGX loading and CUDA project-ID initialization")
  endif()
endif()
include(FindPackageHandleStandardArgs)
find_package_handle_standard_args(DLSS REQUIRED_VARS DLSS_INCLUDE_DIR)
mark_as_advanced(DLSS_INCLUDE_DIR)
