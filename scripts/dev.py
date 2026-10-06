"""Start the local backend and Streamlit UI; Ctrl+C stops both."""
import subprocess
import sys
import time
from app.config import ROOT_DIR


def main():
    processes = []
    try:
        for args in (['uvicorn', 'app.api.main:app', '--host', '127.0.0.1', '--port', '8000'],
                     ['streamlit', 'run', 'ui/streamlit_app.py', '--server.address', '127.0.0.1', '--server.port', '8501', '--server.headless', 'true']):
            processes.append(subprocess.Popen([sys.executable, '-m', *args], cwd=ROOT_DIR))
        print('Platform: http://127.0.0.1:8000/docs\nUI: http://127.0.0.1:8501', flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    raise SystemExit(main())
