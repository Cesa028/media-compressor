import runpy, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
runpy.run_path("media_compressor.py", run_name="__main__")
