# SPDX-License-Identifier: Apache-2.0
"""Rebuild the two historical split-only controls in their benchmark workspace."""
import argparse
from pathlib import Path
import ctypes,json,re,subprocess,hashlib,os
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("benchmark_directory", type=Path, help="Original light-tree benchmark directory with bin/reference and bin/pristine")
parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[3])
args = parser.parse_args()
HERE = args.benchmark_directory.resolve(strict=True)
ROOT = args.source_root.resolve(strict=True)
if not (HERE / "bin/reference/light_tree-compile.txt").is_file():
    parser.error("The recorded reference build commands are missing from the benchmark directory.")
# Compiler work inherits separate cores from the measured renderer processes.
kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.GetCurrentProcess.restype=ctypes.c_void_p;kernel.SetProcessAffinityMask.argtypes=[ctypes.c_void_p,ctypes.c_size_t]
assert kernel.SetProcessAffinityMask(kernel.GetCurrentProcess(),0x50)
for source_arm,label in (('reference','reference-split'),('pristine','pristine-split')):
 src=(HERE/'reference-src/light_tree.cpp') if source_arm=='reference' else (ROOT/'build/stock-cycles-74e9d9e4/source/src/scene/light_tree.cpp')
 content=src.read_text();prefix='Reference' if source_arm=='reference' else ''
 old=f'''    std::array<{prefix}LightTreeBucket, {prefix}LightTreeBucket::num_buckets - 1> right_buckets;
    right_buckets.back() = buckets.back();
    for (int i = {prefix}LightTreeBucket::num_buckets - 3; i >= 0; i--) {{
      right_buckets[i] = right_buckets[i + 1] + buckets[i + 1];
    }}'''
 new=f'''    std::array<{prefix}LightTreeMeasure, {prefix}LightTreeBucket::num_buckets - 1> right_measures;
    right_measures.back() = buckets.back().measure;
    for (int i = {prefix}LightTreeBucket::num_buckets - 3; i >= 0; i--) {{
      right_measures[i] = right_measures[i + 1] + buckets[i + 1].measure;
    }}'''
 assert old in content;content=content.replace(old,new)
 old=f'    for (int split = 0; split < {prefix}LightTreeBucket::num_buckets - 1; split++) {{'
 assert old in content;content=content.replace(old,old+'\n      if (split > 0 && buckets[split].count == 0) continue;')
 content=content.replace('right_buckets[split].measure.calculate()','right_measures[split].calculate()')
 dest=HERE/'bin'/label;dest.mkdir(parents=True,exist_ok=True);source=dest/'light_tree.cpp';source.write_text(content);output=dest/'light_tree.obj'
 if source_arm=='reference':command=(HERE/'bin/reference/light_tree-compile.txt').read_text()
 else:
  commands=json.loads((ROOT/'build/stock-cycles-74e9d9e4/build/compile_commands.json').read_text())
  command=next(x['command'] for x in commands if x['file'].replace('\\','/').endswith('/scene/light_tree.cpp'))
 command=re.sub(r' /Fo(?:"[^"]*"|\S+)',f' /Fo"{output.as_posix()}"',command);command=re.sub(r' /Fd(?:"[^"]*"|\S+)',f' /Fd"{dest.as_posix()}/"',command);command=command[:command.rfind(' -c ')]+f' -c "{source}"'
 (dest/'compile.txt').write_text(command);subprocess.run(command,cwd=HERE,check=True)
 link=(HERE/'bin'/source_arm/'link.txt').read_text()
 link=link.replace(str(HERE/'bin'/source_arm/'benchmark.exe'),str(dest/'benchmark.exe')).replace(str(HERE/'bin'/source_arm/'benchmark.map'),str(dest/'benchmark.map'))
 if source_arm=='reference':link=link.replace(str(HERE/'bin/reference/light_tree.obj'),str(output))
 else:link=link.replace(' /OUT:',f' "{output}" /OUT:',1)
 (dest/'link.txt').write_text(link);subprocess.run(link,cwd=HERE,check=True)
 (dest/'identity.json').write_text(json.dumps({'label':label,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'executable_sha256':hashlib.sha256((dest/'benchmark.exe').read_bytes()).hexdigest(),'object_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'parent_arm':source_arm},indent=2))
