/* SPDX-License-Identifier: Apache-2.0 */
#include "device/device.h"
#include "kernel/types.h"
#include "scene/camera.h"
#include "scene/film.h"
#include "scene/integrator.h"
#include "scene/mesh.h"
#include "scene/object.h"
#include "scene/pass.h"
#include "scene/scene.h"
#include "scene/shader.h"
#include "scene/shader_graph.h"
#include "scene/shader_nodes.h"
#include "session/buffers.h"
#include "session/output_driver.h"
#include "session/session.h"
#include "util/path.h"
#include "util/task.h"
#include "util/transform.h"
#include <chrono>
#include <fstream>
#include <iostream>
#include <numeric>
#include <windows.h>
using namespace ccl;
struct Capture : OutputDriver {
  std::vector<float> pixels;
  std::chrono::steady_clock::time_point started;
  double first_frame_ms = -1;
  void write_render_tile(const Tile &tile) override {
    if (first_frame_ms < 0)
      first_frame_ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count();
    pixels.resize(tile.size.x * tile.size.y * 4);
    if (!tile.get_pass_pixels("combined", 4, pixels.data()))
      throw std::runtime_error("Missing combined");
  }
};
int main(int argc, char **argv) {
  try {
    if (argc != 5)
      throw std::runtime_error(
          "scene output-prefix "
          "state(0=history,1=fresh-cold,2=fresh-move,3=fresh-full-palette,4=fresh-full-palette-move) samples");
    SetPriorityClass(GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS);
    DWORD_PTR proc_mask, sys_mask;
    GetProcessAffinityMask(GetCurrentProcess(), &proc_mask, &sys_mask);
    std::string mode = argv[1], out = argv[2];
    int fresh = std::stoi(argv[3]), spp = std::stoi(argv[4]);
    bool palette = mode == "regression" || mode == "mixed" || mode == "ordinary" || mode == "receiver-palettes",
         heavy = mode == "regression" || mode == "mixed" || mode == "uniform";
    bool receiver = mode == "receiver" || mode == "receiver-palettes";
    path_init(std::getenv("K32_RESOURCES"), std::getenv("K32_OUTPUT"));
    SessionParams params;
    params.background = true;
    params.samples = spp;
    params.threads = 0;
    params.use_auto_tile = false;

    const std::string target = std::getenv("K32_DEVICE");
    bool found = false;
    for (const auto &info : Device::available_devices(target == "CPU" ? DEVICE_MASK_CPU : DEVICE_MASK_OPTIX)) {
      if (info.id == target || (target == "CPU" && info.type == DEVICE_CPU)) {
        params.device = info;
        found = true;
        break;
      }
    }
    if (!found)
      throw std::runtime_error("Requested device missing: " + target);
    std::cerr << "K32 " << params.device.id << " threads " << params.threads << " affinity " << proc_mask << std::endl;

    SceneParams sceneparams;
    Session session(params, sceneparams);
    Scene *scene = session.scene.get();
    scene->integrator->set_use_light_tree(true);
    scene->integrator->set_use_adaptive_sampling(false);
    scene->integrator->set_seed(7);
    scene->integrator->set_max_bounce(2);
    scene->camera->set_full_width(192);
    scene->camera->set_full_height(144);
    scene->camera->set_camera_type(CAMERA_ORTHOGRAPHIC);
    scene->camera->set_matrix(transform_translate(make_float3(0, 0, 12)) * transform_scale(make_float3(1, 1, -1)));
    scene->camera->compute_auto_viewplane();
    scene->camera->set_viewplane_left(-6);
    scene->camera->set_viewplane_right(6);
    scene->camera->set_viewplane_bottom(-4.5);
    scene->camera->set_viewplane_top(4.5);
    Pass *pass = scene->create_node<Pass>();
    pass->set_name(ustring("combined"));
    pass->set_type(PASS_COMBINED);
    std::vector<Shader *> shaders;
    std::vector<EmissionNode *> nodes;
    const float3 colors[] = {make_float3(.2, .4, .6), make_float3(.8, .1, .2), make_float3(.1, .7, .3),
                             make_float3(.6, .2, .8)};
    for (int i = 0; i < (palette ? 4 : 1); i++) {
      Shader *s = scene->create_node<Shader>();
      auto g = make_unique<ShaderGraph>();
      auto *n = g->create_node<EmissionNode>();
      n->set_color(colors[i] + make_float3(fresh >= 3 ? .03f : 0, 0, 0));
      n->set_strength(1);
      g->connect(n->output("Emission"), g->output()->input("Surface"));
      s->set_graph(std::move(g));
      s->tag_update(scene);
      shaders.push_back(s);
      nodes.push_back(n);
    }
    auto grid = [&](int rows, int cols, float sx, float sy, Shader *shader) {
      Mesh *m = scene->create_node<Mesh>();
      m->resize_mesh(rows * cols, 2 * (rows - 1) * (cols - 1));
      auto *v = m->get_position_for_write();
      for (int y = 0; y < rows; y++)
        for (int x = 0; x < cols; x++)
          v[y * cols + x] = make_float3(sx * (float(x) / (cols - 1) - .5f), sy * (float(y) / (rows - 1) - .5f), 0);
      auto *t = m->get_triangles().data();
      int k = 0;
      for (int y = 0; y < rows - 1; y++)
        for (int x = 0; x < cols - 1; x++) {
          int a = y * cols + x, b = a + 1, c = a + cols, d = c + 1;
          t[k++] = a;
          t[k++] = b;
          t[k++] = d;
          t[k++] = a;
          t[k++] = d;
          t[k++] = c;
        }
      std::fill(m->get_shader().begin(), m->get_shader().end(), 0);
      std::fill(m->get_smooth().begin(), m->get_smooth().end(), false);
      array<Node *> used;
      used.resize(1);
      used[0] = shader;
      m->set_used_shaders(used);
      m->tag_triangles_modified();
      m->tag_shader_modified();
      m->compute_bounds();
      return m;
    };
    Mesh *mesh = grid(heavy ? 513 : 3, heavy ? 1025 : 3, 1.8f, 1.6f, shaders[0]);
    std::vector<Object *> objects;
    for (int i = 0; i < 12; i++) {
      Object *o = scene->create_node<Object>();
      o->set_geometry(mesh);
      o->set_tfm(transform_translate(make_float3(-4.5f + 3 * (i % 4) +
                                                     (fresh == 4   ? .045f
                                                      : fresh >= 2 ? .03f
                                                                   : 0),
                                                 -3 + 3 * (i / 4), receiver ? 2 : 0)));
#ifndef PRISTINE
      if (palette) {
        array<Node *> p;
        p.resize(1);
        p[0] = shaders[i % 4];
        o->set_shader_palette(p);
      }
#endif
      o->tag_update(scene);
      objects.push_back(o);
    }
    if (receiver) {
      Shader *s = scene->create_node<Shader>();
      auto g = make_unique<ShaderGraph>();
      auto *n = g->create_node<DiffuseBsdfNode>();
      n->set_color(make_float3(.8, .8, .8));
      g->connect(n->output("BSDF"), g->output()->input("Surface"));
      s->set_graph(std::move(g));
      s->tag_update(scene);
      Mesh *m = grid(3, 3, 12, 9, s);
      Object *o = scene->create_node<Object>();
      o->set_geometry(m);
      o->set_tfm(transform_identity());
      o->tag_update(scene);
      // Hide the emissive plane from camera rays so receiver pixels exercise transport.
      for (Object *e : objects)
        e->set_visibility(PATH_RAY_VISIBILITY_ALL & ~PATH_RAY_VISIBILITY_CAMERA);
    }
    auto capture = make_unique<Capture>();
    Capture *driver = capture.get();
    session.set_output_driver(std::move(capture));
    BufferParams buffer;
    buffer.width = buffer.full_width = 192;
    buffer.height = buffer.full_height = 144;
    auto render = [&](std::string label) {
      auto start = std::chrono::steady_clock::now();
      driver->started = start;
      driver->first_frame_ms = -1;
      session.reset(params, buffer);
      session.start();
      session.wait();
      double ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - start).count();
      if (session.progress.get_error())
        throw std::runtime_error(session.progress.get_error_message());
      if (driver->pixels.size() != 192 * 144 * 4)
        throw std::runtime_error("image size");
      std::ofstream image(out + "-" + label + ".f32", std::ios::binary);
      image.write(reinterpret_cast<char *>(driver->pixels.data()), driver->pixels.size() * sizeof(float));
      std::cout << "MEASURE {\"edit\":\"" << label << "\",\"ms\":" << ms
                << ",\"first_frame_ms\":" << driver->first_frame_ms << ",\"pixels\":" << driver->pixels.size() << "}"
                << std::endl;
    };
    render(fresh == 1   ? "cold"
           : fresh == 2 ? "cold-move"
           : fresh == 3 ? "full-palette"
           : fresh == 4 ? "full-palette-move"
                        : "cold");
    if (!fresh) {
      auto move = [&](float offset) {
        for (int i = 0; i < 12; i++) {
          objects[i]->set_tfm(
              transform_translate(make_float3(-4.5f + 3 * (i % 4) + offset, -3 + 3 * (i / 4), receiver ? 2 : 0)));
          objects[i]->tag_update(scene);
        }
      };
      move(.03f);
      render("cold-move");
      for (int i = 0; i < nodes.size(); i++) {
        nodes[i]->set_color(colors[i] + make_float3(.03f, 0, 0));
        shaders[i]->tag_update(scene);
      }
      render("full-palette");
      move(.045f);
      render("full-palette-move");
    }
    return 0;
  } catch (std::exception &e) {
    std::cerr << "ERROR " << e.what() << std::endl;
    return 2;
  }
}
