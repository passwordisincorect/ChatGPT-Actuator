"""Match the executed Python target, never arbitrary command-line arguments."""
import os


def python_target(argv,cwd=None):
    index = 1
    while index < len(argv):
        value = argv[index]
        if value == '-c':
            return None
        if value == '-m':
            return ('module', argv[index + 1]) if index + 1 < len(argv) else None
        if value in {'-X', '-W'}:
            index += 2
            continue
        if value == '--':
            index += 1
            break
        if value.startswith('-'):
            index += 1
            continue
        break
    return ('file', os.path.normcase(os.path.abspath(os.path.join(cwd or os.getcwd(),argv[index])))) if index < len(argv) else None


def target_for(script):
    if str(script) == 'chatgpt_actuator.control_center':
        return ('module', str(script))
    return ('file', os.path.normcase(os.path.abspath(str(script))))
