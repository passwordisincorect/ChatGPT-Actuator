"""Owner-approved fixed workloads. No shell, flashing, or desktop service launch."""
import hashlib
from pathlib import Path
import re

def checksum(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def stm32_profiles(build, cmake, ctest):
    build=Path(build).resolve(strict=True)
    cache=build/'CMakeCache.txt'
    text=cache.read_text(encoding='utf-8',errors='strict')
    compiler=re.search(r'^CMAKE_C_COMPILER:FILEPATH=(.+)$',text,re.M)
    source=re.search(r'^CMAKE_HOME_DIRECTORY:INTERNAL=(.+)$',text,re.M)
    if not compiler or not source or not any(x in Path(compiler[1].strip()).name.lower() for x in ('arm-none-eabi','armclang')):
        raise ValueError('Select an already configured STM32/ARM CMake build')
    project=Path(source[1].strip()).resolve(strict=True)
    if not (project/'CMakeLists.txt').is_file(): raise ValueError('Missing CMake project')
    inputs=[cache,*project.rglob('CMakeLists.txt'),*project.rglob('*.cmake')]
    inputs += [p for p in (build/'build.ninja',build/'Makefile',build/'CTestTestfile.cmake') if p.is_file()]
    guards={str(p.resolve()):checksum(p) for p in inputs if p.is_file()}
    result={}
    for name,exe,args in (
        ('stm32-build',cmake,['--build',str(build),'--parallel','2']),
        ('stm32-test',ctest,['--test-dir',str(build),'--output-on-failure','--timeout','30'])):
        exe=Path(exe).resolve(strict=True)
        if not exe.is_file():raise ValueError('Missing build/test executable')
        result[name]={'executable':str(exe),'arguments':args,'cwd':str(build),
            'executable_sha256':checksum(exe),
            'background_approved':True,'timeout_seconds':300,'max_output_bytes':262144,'input_sha256':guards}
    return result
