# SPDX-License-Identifier: Apache-2.0
# Entered from the patched Cycles src directory: inherit its exact compile definitions.
file(READ "${CYCLES_HOUDINI_INTEGRATION_ROOT}/source.lock.json" CH_SOURCE_LOCK)
configure_file("${CYCLES_HOUDINI_INTEGRATION_ROOT}/source.lock.json" "${CMAKE_BINARY_DIR}/source.lock.json" COPYONLY)
string(JSON CH_ENGINE_UPSTREAM GET "${CH_SOURCE_LOCK}" cycles commit)
set_property(SOURCE core/session.cpp APPEND PROPERTY COMPILE_DEFINITIONS CH_ENGINE_UPSTREAM="${CH_ENGINE_UPSTREAM}")
if(NOT TARGET Houdini)
  find_package(Houdini REQUIRED CONFIG PATHS "${HOUDINI_ROOT}/toolkit/cmake" NO_DEFAULT_PATH)
endif()
message(STATUS "Cycles Houdini integration: ${CYCLES_HOUDINI_INTEGRATION_ROOT}")

set(CH_CYCLES_SOURCE "${CH_ENGINE_ROOT}/src")
set(CH_ENGINE_LIBRARIES cycles_device cycles_kernel cycles_scene cycles_session
    cycles_bvh cycles_subd cycles_graph cycles_util extern_sky)
cycles_external_libraries_append(CH_ENGINE_LIBRARIES)
add_library(ch_core STATIC core/session.cpp core/render_devices.cpp core/render_settings.cpp core/denoising.cpp core/fixture.cpp core/color.cpp core/color_interop.cpp core/volume.cpp core/staged_output.cpp core/memory.cpp core/shader.cpp core/shader_graph.cpp core/texture_assets.cpp core/snapshot.cpp core/snapshot_wire.cpp)
target_sources(ch_core PRIVATE
    core/session_validation.cpp
    core/session_preparation.cpp
    core/session_shaders.cpp
    core/session_geometry.cpp
    core/session_displacement.cpp
    core/session_strands.cpp
    core/session_lights.cpp
    core/session_camera.cpp
    core/session_transaction.cpp)
target_sources(ch_core PRIVATE core/shader_curves.cpp)
target_sources(ch_core PRIVATE core/shader_switches.cpp)
target_sources(ch_core PRIVATE core/shader_primitives.cpp)
if(MSVC)
  # Endpoint differences are amplified by the shader's linear extrapolation.
  # Preserve ordered float arithmetic in the bounded host/worker table builder.
  set_source_files_properties(core/shader_curves.cpp PROPERTIES COMPILE_OPTIONS "/fp:precise")
endif()
target_sources(ch_core PRIVATE core/image.cpp)
target_sources(ch_core PRIVATE core/pass_catalogue.cpp core/exr_output.cpp core/display_image.cpp core/dicing_policy.cpp)
target_sources(ch_core PRIVATE core/texture_memory.cpp)
target_sources(ch_core PRIVATE core/ies_assets.cpp)
target_sources(ch_core PRIVATE core/linking.cpp)
target_sources(ch_core PRIVATE core/camera_motion.cpp)
target_sources(ch_core PRIVATE core/camera_projection.cpp)

# Optional native image aperture. The disabled implementation has no image I/O
# or kernel dependency and rejects requested image modes explicitly.
target_sources(ch_core PRIVATE features/image_iris/image_iris.cpp)

# Certified native spline math is compiled separately from Cycles' SIMD headers.
target_link_libraries(ch_core PRIVATE ch_ramp_math)
target_compile_features(ch_core PUBLIC cxx_std_20)
target_include_directories(ch_core PUBLIC "${CYCLES_HOUDINI_INTEGRATION_ROOT}/include" PRIVATE "${CH_CYCLES_SOURCE}")
if(WITH_CYCLES_PATH_GUIDING)
  # Engine integrator headers include OpenPGL when guiding is compiled in.
  # Consumers of ch_core compile those headers too, including ch_harness.
  target_include_directories(ch_core SYSTEM PUBLIC ${OPENPGL_INCLUDE_DIR})
endif()
target_link_libraries(ch_core PRIVATE ${CH_ENGINE_LIBRARIES})
if(NOT TARGET OpenEXR::OpenEXRCore)
  find_library(CH_OPENEXR_CORE NAMES OpenEXRCore_sidefx
    PATHS "${HOUDINI_ROOT}/custom/houdini/dsolib" NO_DEFAULT_PATH REQUIRED)
  add_library(OpenEXR::OpenEXRCore UNKNOWN IMPORTED)
  set_target_properties(OpenEXR::OpenEXRCore PROPERTIES IMPORTED_LOCATION "${CH_OPENEXR_CORE}")
endif()
target_link_libraries(ch_core PRIVATE OpenEXR::OpenEXRCore)
target_include_directories(ch_core SYSTEM PRIVATE
    "${HOUDINI_ROOT}/toolkit/include/OpenEXR"
    "${HOUDINI_ROOT}/toolkit/include/Imath")
add_library(ch_platform STATIC)
target_compile_features(ch_platform PUBLIC cxx_std_20)
if(WIN32)
  target_sources(ch_platform PRIVATE platform/win32.cpp)
  target_link_libraries(ch_platform PRIVATE dxgi)
else()
  target_sources(ch_platform PRIVATE platform/posix.cpp)
endif()
target_link_libraries(ch_core PRIVATE ch_platform)
add_executable(ch_harness harness/main.cpp)
target_sources(ch_harness PRIVATE harness/dispersion_contracts.cpp)
target_sources(ch_harness PRIVATE harness/gpu_concurrency_contracts.cpp)
target_sources(ch_harness PRIVATE harness/ies_ownership_contracts.cpp)
target_sources(ch_harness PRIVATE harness/subdivision_shading_contracts.cpp)

target_sources(ch_harness PRIVATE harness/image_iris_contracts.cpp)
target_sources(ch_harness PRIVATE harness/image_iris_render.cpp)
target_sources(ch_harness PRIVATE harness/image_iris_color_render.cpp)

option(CH_NATIVE_ASAN_CONTRACTS "Instrument native diagnostic contracts and worker with MSVC AddressSanitizer" OFF)
if(CH_NATIVE_ASAN_CONTRACTS)
  if(NOT MSVC)
    message(FATAL_ERROR "CH_NATIVE_ASAN_CONTRACTS currently requires MSVC")
  endif()
  # Pinned third-party libraries were built without STL container annotations.
  foreach(contract_target ch_harness ch_core cycles_scene cycles_subd)
    target_compile_options(${contract_target} PRIVATE /fsanitize=address /Zi)
    target_compile_definitions(${contract_target} PRIVATE _DISABLE_VECTOR_ANNOTATION _DISABLE_STRING_ANNOTATION)
  endforeach()
  target_link_options(ch_harness PRIVATE /INCREMENTAL:NO /DEBUG)
endif()
target_compile_features(ch_harness PRIVATE cxx_std_20)
target_include_directories(ch_harness SYSTEM PRIVATE ${OPENVDB_INCLUDE_DIRS} ${TBB_INCLUDE_DIRS} ${EMBREE_INCLUDE_DIRS})
target_include_directories(ch_harness PRIVATE "${CH_CYCLES_SOURCE}")
target_link_libraries(ch_harness PRIVATE ch_core)
set_target_properties(ch_harness PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
if(MSVC)
  # Keep standalone native contract failures symbolizable in Release builds.
  target_link_options(ch_harness PRIVATE "/MAP:${CMAKE_BINARY_DIR}/bin/ch_harness.map")
endif()
add_executable(ch_volume_lookup_probe harness/volume_lookup_probe.cpp)
target_include_directories(ch_volume_lookup_probe PRIVATE "${CH_CYCLES_SOURCE}" "${CYCLES_HOUDINI_INTEGRATION_ROOT}/include")
target_link_libraries(ch_volume_lookup_probe PRIVATE ${CH_ENGINE_LIBRARIES})
set_target_properties(ch_volume_lookup_probe PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

# One host DLL is both the HDK DSO and the CPython extension. Loading the same absolute
# file keeps the session registry and Cycles globals singular inside Houdini.
add_library(_cycles_houdini SHARED houdini/scene_adapter.cpp houdini/geometry_selection.cpp houdini/mesh_adapter.cpp houdini/point_curve_adapter.cpp houdini/deformation_adapter.cpp houdini/volume_adapter.cpp houdini/packed_adapter.cpp houdini/python_module.cpp houdini/plugin.cpp houdini/tile_display.cpp houdini/material_vop.cpp houdini/material_subnet.cpp houdini/material_assignment.cpp houdini/texture_policy.cpp houdini/scene_persistence.cpp)
target_sources(_cycles_houdini PRIVATE houdini/material_snapshot.cpp houdini/python_status.cpp houdini/python_bindings.cpp)
target_sources(_cycles_houdini PRIVATE houdini/python_session.cpp houdini/python_scene.cpp
    houdini/python_snapshot.cpp houdini/python_frame.cpp)
target_sources(_cycles_houdini PRIVATE houdini/native_external_binding.cpp)
target_sources(_cycles_houdini PRIVATE houdini/material_motion.cpp)
target_sources(_cycles_houdini PRIVATE houdini/point_instancer.cpp)

target_sources(_cycles_houdini PRIVATE houdini/blend_mesh.cpp)
target_sources(_cycles_houdini PRIVATE houdini/packed_blender_reference.cpp houdini/sop_blender_reference.cpp)
target_sources(_cycles_houdini PRIVATE houdini/packed_fragments.cpp)
target_sources(_cycles_houdini PRIVATE houdini/packed_blender_bounds.cpp)
target_sources(_cycles_houdini PRIVATE houdini/material_stylesheets.cpp)
target_sources(_cycles_houdini PRIVATE houdini/stylesheet_sources.cpp)
target_sources(_cycles_houdini PRIVATE houdini/stylesheet_evaluation.cpp)
target_sources(_cycles_houdini PRIVATE houdini/material_preview.cpp)
target_sources(_cycles_houdini PRIVATE houdini/cop_images.cpp)
# Pinned header-only pybind11 2.13.6; retain Houdini's exact Python ABI.
add_library(ch_pybind11 INTERFACE)
target_include_directories(ch_pybind11 SYSTEM INTERFACE "${CYCLES_HOUDINI_INTEGRATION_ROOT}/include/third_party")
target_link_libraries(_cycles_houdini PRIVATE ch_pybind11)
target_include_directories(_cycles_houdini PRIVATE "${HOUDINI_ROOT}/toolkit/include/python3.13")
target_link_directories(_cycles_houdini PRIVATE "${HOUDINI_ROOT}/python313/libs")

# A real CPython probe of the migrating bindings, with pure fixture values and
# no HDK/engine startup. Keep its identically named module isolated from the DSO.
add_library(ch_python_bindings_contracts MODULE harness/python_bindings_contract_module.cpp
    houdini/python_status.cpp houdini/python_bindings.cpp)
target_compile_features(ch_python_bindings_contracts PRIVATE cxx_std_20)
target_include_directories(ch_python_bindings_contracts PRIVATE
    "${CYCLES_HOUDINI_INTEGRATION_ROOT}/include" "${HOUDINI_ROOT}/toolkit/include/python3.13")
target_link_directories(ch_python_bindings_contracts PRIVATE "${HOUDINI_ROOT}/python313/libs")
target_link_libraries(ch_python_bindings_contracts PRIVATE ch_pybind11 python313)
set_target_properties(ch_python_bindings_contracts PROPERTIES PREFIX "" OUTPUT_NAME "_cycles_houdini"
    SUFFIX ".pyd" LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/binding-contracts"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/binding-contracts")
target_link_libraries(_cycles_houdini PRIVATE ch_core Houdini)
if(MSVC AND WITH_CYCLES_EMBREE)
  # Houdini loads this DSO at startup to register nodes. Only rendering needs
  # Embree and its SYCL runtime. Keep the worker's ordinary Embree import.
  target_link_options(_cycles_houdini PRIVATE "/DELAYLOAD:embree4.dll")
  target_link_libraries(_cycles_houdini PRIVATE delayimp)
endif()
if(NOT CH_NATIVE_HOST_ROOT)
    set(CH_NATIVE_HOST_ROOT "${CMAKE_BINARY_DIR}/houdini")
endif()
houdini_configure_target(_cycles_houdini INSTDIR "${CH_NATIVE_HOST_ROOT}/dso")
# Wire codec is platform-neutral; the backend owns all mapping/pipe handles.
add_library(ch_ipc STATIC ipc/control_pipe.cpp ipc/win32.cpp)
target_compile_features(ch_ipc PUBLIC cxx_std_20)
target_include_directories(ch_ipc PUBLIC "${CYCLES_HOUDINI_INTEGRATION_ROOT}/include")
add_executable(ch_ipc_contracts harness/ipc_contracts.cpp)
target_link_libraries(ch_ipc_contracts PRIVATE ch_ipc)
set_target_properties(ch_ipc_contracts PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_executable(ch_worker worker/main.cpp)
if(CH_NATIVE_ASAN_CONTRACTS)
  target_compile_options(ch_worker PRIVATE /fsanitize=address /Zi)
  target_compile_definitions(ch_worker PRIVATE _DISABLE_VECTOR_ANNOTATION _DISABLE_STRING_ANNOTATION)
  target_link_options(ch_worker PRIVATE /INCREMENTAL:NO /DEBUG)
endif()
target_compile_features(ch_worker PRIVATE cxx_std_20)
target_include_directories(ch_worker SYSTEM PRIVATE "${HOUDINI_ROOT}/toolkit/include")
target_link_libraries(ch_worker PRIVATE ch_core ch_ipc ch_platform)
set_target_properties(ch_worker PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CH_NATIVE_HOST_ROOT}/bin")

add_executable(ch_dicing_contracts harness/dicing_contracts.cpp core/dicing_policy.cpp)
target_compile_features(ch_dicing_contracts PRIVATE cxx_std_20)
target_include_directories(ch_dicing_contracts PRIVATE "${CYCLES_HOUDINI_INTEGRATION_ROOT}/include")
set_target_properties(ch_dicing_contracts PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

add_executable(ch_frame_publication_contracts harness/frame_publication_contracts.cpp)
target_compile_features(ch_frame_publication_contracts PRIVATE cxx_std_20)
target_include_directories(ch_frame_publication_contracts PRIVATE "${CH_CYCLES_SOURCE}")
target_include_directories(ch_frame_publication_contracts SYSTEM PRIVATE "${HOUDINI_ROOT}/toolkit/include")
target_link_libraries(ch_frame_publication_contracts PRIVATE ch_core)
set_target_properties(ch_frame_publication_contracts PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

add_executable(ch_platform_contracts harness/platform_contracts.cpp)
target_link_libraries(ch_platform_contracts PRIVATE ch_platform)
set_target_properties(ch_platform_contracts PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
