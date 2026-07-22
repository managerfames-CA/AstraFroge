# BE-24 Completion Evidence: Docker Build Verification

This document provides evidence of successful Docker build verification for the AstraForge Crypto Backend (Render-ready).

## Docker Build Logs

The following is the output from running a clean `--no-cache` Docker build:

```console
$ docker build --no-cache -t test-build .
DEPRECATED: The legacy builder is deprecated and will be removed in a future release.
            BuildKit is currently disabled; enable it by removing the DOCKER_BUILDKIT=0
            environment-variable.

Sending build context to Docker daemon  5.733MB
Step 1/12 : FROM python:3.12-slim AS runtime
 ---> 25c5b8011a34
Step 2/12 : ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PIP_NO_CACHE_DIR=1
 ---> Running in 876e1eb4bedd
 ---> Removed intermediate container 876e1eb4bedd
 ---> d15503061f3d
Step 3/12 : WORKDIR /app
 ---> Running in fbe6a961effc
 ---> Removed intermediate container fbe6a961effc
 ---> 798b77e16a22
Step 4/12 : RUN addgroup --system astrforge && adduser --system --ingroup astrforge astrforge
 ---> Running in 2eecaffcc3d6
 ---> Removed intermediate container 2eecaffcc3d6
 ---> 93bf0b091670
Step 5/12 : COPY pyproject.toml README.md ./
 ---> c57715f3c21f
Step 6/12 : COPY app ./app
 ---> 09e4027ca6cc
Step 7/12 : COPY alembic.ini ./
 ---> 02da62c4132b
Step 8/12 : COPY migrations ./migrations
 ---> 39fca4f8f34d
Step 9/12 : RUN python -m pip install --upgrade pip && python -m pip install .
 ---> Running in 1f8471796611
Requirement already satisfied: pip in /usr/local/lib/python3.12/site-packages (25.0.1)
Collecting pip
  Downloading pip-26.1.2-py3-none-any.whl.metadata (4.6 kB)
Downloading pip-26.1.2-py3-none-any.whl (1.8 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 1.8/1.8 MB 46.1 MB/s eta 0:00:00
Installing collected packages: pip
  Attempting uninstall: pip
    Found existing installation: pip 25.0.1
    Uninstalling pip-25.0.1:
      Successfully uninstalled pip-25.0.1
Successfully installed pip-26.1.2
WARNING: Running pip as the 'root' user can result in broken permissions and conflicting behaviour with the system package manager, possibly rendering your system unusable. It is recommended to use a virtual environment instead: https://pip.pypa.io/warnings/venv. Use the --root-user-action option if you know what you are doing and want to suppress this warning.
Processing ./.
  Installing build dependencies: started
  Installing build dependencies: finished with status 'done'
  Getting requirements to build wheel: started
  Getting requirements to build wheel: finished with status 'done'
  Preparing metadata (pyproject.toml): started
  Preparing metadata (pyproject.toml): finished with status 'done'
Collecting fastapi<1,>=0.115 (from astraforge-backend==0.4.0)
  Downloading fastapi-0.139.2-py3-none-any.whl.metadata (26 kB)
Collecting httpx<1,>=0.28 (from astraforge-backend==0.4.0)
  Downloading httpx-0.28.1-py3-none-any.whl.metadata (7.1 kB)
Collecting pydantic-settings<3,>=2.6 (from astraforge-backend==0.4.0)
  Downloading pydantic_settings-2.14.2-py3-none-any.whl.metadata (3.4 kB)
Collecting sqlalchemy<3,>=2.0.36 (from astraforge-backend==0.4.0)
  Downloading sqlalchemy-2.0.51-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl.metadata (9.5 kB)
Collecting psycopg<4,>=3.2 (from psycopg[binary]<4,>=3.2->astraforge-backend==0.4.0)
  Downloading psycopg-3.3.4-py3-none-any.whl.metadata (4.3 kB)
Collecting alembic<2,>=1.14 (from astraforge-backend==0.4.0)
  Downloading alembic-1.18.5-py3-none-any.whl.metadata (7.2 kB)
Collecting uvicorn<1,>=0.32 (from uvicorn[standard]<1,>=0.32->astraforge-backend==0.4.0)
  Downloading uvicorn-0.51.0-py3-none-any.whl.metadata (6.6 kB)
Collecting Mako (from alembic<2,>=1.14->astraforge-backend==0.4.0)
  Downloading mako-1.3.12-py3-none-any.whl.metadata (2.9 kB)
Collecting typing-extensions>=4.12 (from alembic<2,>=1.14->astraforge-backend==0.4.0)
  Downloading typing_extensions-4.16.0-py3-none-any.whl.metadata (3.3 kB)
Collecting starlette>=0.46.0 (from fastapi<1,>=0.115->astraforge-backend==0.4.0)
  Downloading starlette-1.3.1-py3-none-any.whl.metadata (6.4 kB)
Collecting pydantic>=2.9.0 (from fastapi<1,>=0.115->astraforge-backend==0.4.0)
  Downloading pydantic-2.13.4-py3-none-any.whl.metadata (109 kB)
Collecting typing-inspection>=0.4.2 (from fastapi<1,>=0.115->astraforge-backend==0.4.0)
  Downloading typing_inspection-0.4.2-py3-none-any.whl.metadata (2.6 kB)
Collecting annotated-doc>=0.0.2 (from fastapi<1,>=0.115->astraforge-backend==0.4.0)
  Downloading annotated_doc-0.0.4-py3-none-any.whl.metadata (6.6 kB)
Collecting anyio (from httpx<1,>=0.28->astraforge-backend==0.4.0)
  Downloading anyio-4.14.2-py3-none-any.whl.metadata (4.6 kB)
Collecting certifi (from httpx<1,>=0.28->astraforge-backend==0.4.0)
  Downloading certifi-2026.7.22-py3-none-any.whl.metadata (2.5 kB)
Collecting httpcore==1.* (from httpx<1,>=0.28->astraforge-backend==0.4.0)
  Downloading httpcore-1.0.9-py3-none-any.whl.metadata (21 kB)
Collecting idna (from httpx<1,>=0.28->astraforge-backend==0.4.0)
  Downloading idna-3.18-py3-none-any.whl.metadata (6.1 kB)
Collecting h11>=0.16 (from httpcore==1.*->httpx<1,>=0.28->astraforge-backend==0.4.0)
  Downloading h11-0.16.0-py3-none-any.whl.metadata (8.3 kB)
Collecting psycopg-binary==3.3.4 (from psycopg[binary]<4,>=3.2->astraforge-backend==0.4.0)
  Downloading psycopg_binary-3.3.4-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl.metadata (2.7 kB)
Collecting python-dotenv>=0.21.0 (from pydantic-settings<3,>=2.6->astraforge-backend==0.4.0)
  Downloading python_dotenv-1.2.2-py3-none-any.whl.metadata (27 kB)
Collecting greenlet>=1 (from sqlalchemy<3,>=2.0.36->astraforge-backend==0.4.0)
  Downloading greenlet-3.5.4-cp312-cp312-manylinux_2_24_x86_64.manylinux_2_28_x86_64.whl.metadata (3.8 kB)
Collecting click>=7.0 (from uvicorn<1,>=0.32->uvicorn[standard]<1,>=0.32->astraforge-backend==0.4.0)
  Downloading click-8.4.2-py3-none-any.whl.metadata (2.6 kB)
Collecting httptools>=0.8.0 (from uvicorn[standard]<1,>=0.32->astraforge-backend==0.4.0)
  Downloading httptools-0.8.0-cp312-cp312-manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64.whl.metadata (3.5 kB)
Collecting pyyaml>=5.1 (from uvicorn[standard]<1,>=0.32->astraforge-backend==0.4.0)
  Downloading pyyaml-6.0.3-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl.metadata (2.4 kB)
Collecting uvloop>=0.15.1 (from uvicorn[standard]<1,>=0.32->astraforge-backend==0.4.0)
  Downloading uvloop-0.22.1-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl.metadata (4.9 kB)
Collecting watchfiles>=0.20 (from uvicorn[standard]<1,>=0.32->astraforge-backend==0.4.0)
  Downloading watchfiles-1.2.0-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl.metadata (4.9 kB)
Collecting websockets>=13.0 (from uvicorn[standard]<1,>=0.32->astraforge-backend==0.4.0)
  Downloading websockets-16.1.1-cp312-cp312-manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64.whl.metadata (6.8 kB)
Collecting annotated-types>=0.6.0 (from pydantic>=2.9.0->fastapi<1,>=0.115->astraforge-backend==0.4.0)
  Downloading annotated_types-0.7.0-py3-none-any.whl.metadata (15 kB)
Collecting pydantic-core==2.46.4 (from pydantic>=2.9.0->fastapi<1,>=0.115->astraforge-backend==0.4.0)
  Downloading pydantic_core-2.46.4-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl.metadata (6.6 kB)
Collecting MarkupSafe>=0.9.2 (from Mako->alembic<2,>=1.14->astraforge-backend==0.4.0)
  Downloading markupsafe-3.0.3-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl.metadata (2.7 kB)
Downloading alembic-1.18.5-py3-none-any.whl (264 kB)
Downloading fastapi-0.139.2-py3-none-any.whl (130 kB)
Downloading httpx-0.28.1-py3-none-any.whl (73 kB)
Downloading httpcore-1.0.9-py3-none-any.whl (78 kB)
Downloading psycopg-3.3.4-py3-none-any.whl (213 kB)
Downloading psycopg_binary-3.3.4-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl (5.2 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 5.2/5.2 MB 95.3 MB/s  0:00:00
Downloading pydantic_settings-2.14.2-py3-none-any.whl (61 kB)
Downloading sqlalchemy-2.0.51-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl (3.4 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 3.4/3.4 MB 74.9 MB/s  0:00:00
Downloading uvicorn-0.51.0-py3-none-any.whl (73 kB)
Downloading annotated_doc-0.0.4-py3-none-any.whl (5.3 kB)
Downloading click-8.4.2-py3-none-any.whl (119 kB)
Downloading greenlet-3.5.4-cp312-cp312-manylinux_2_24_x86_64.manylinux_2_28_x86_64.whl (621 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 621.5/621.5 kB 43.2 MB/s  0:00:00
Downloading h11-0.16.0-py3-none-any.whl (37 kB)
Downloading httptools-0.8.0-cp312-cp312-manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64.whl (523 kB)
Downloading pydantic-2.13.4-py3-none-any.whl (472 kB)
Downloading pydantic_core-2.46.4-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (2.1 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 2.1/2.1 MB 383.8 MB/s  0:00:00
Downloading annotated_types-0.7.0-py3-none-any.whl (13 kB)
Downloading python_dotenv-1.2.2-py3-none-any.whl (22 kB)
Downloading pyyaml-6.0.3-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl (807 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 807.9/807.9 kB 200.6 MB/s  0:00:00
Downloading starlette-1.3.1-py3-none-any.whl (73 kB)
Downloading anyio-4.14.2-py3-none-any.whl (125 kB)
Downloading idna-3.18-py3-none-any.whl (65 kB)
Downloading typing_extensions-4.16.0-py3-none-any.whl (45 kB)
Downloading typing_inspection-0.4.2-py3-none-any.whl (14 kB)
Downloading uvloop-0.22.1-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl (4.4 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 4.4/4.4 MB 87.8 MB/s  0:00:00
Downloading watchfiles-1.2.0-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (456 kB)
Downloading websockets-16.1.1-cp312-cp312-manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64.whl (187 kB)
Downloading certifi-2026.7.22-py3-none-any.whl (136 kB)
Downloading mako-1.3.12-py3-none-any.whl (78 kB)
Downloading markupsafe-3.0.3-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl (22 kB)
Building wheels for collected packages: astraforge-backend
  Building wheel for astraforge-backend (pyproject.toml): started
  Building wheel for astraforge-backend (pyproject.toml): finished with status 'done'
  Created wheel for astraforge-backend: filename=astraforge_backend-0.4.0-py3-none-any.whl size=232507 sha256=1839a162f6d1f0ee134fed397a99b53291154a7bb7c71c1be59a2722a42fa8fb
  Stored in directory: /tmp/pip-ephem-wheel-cache-y6mxd_67/wheels/54/1b/b7/aa63e25c8f14f4f2ae7b04e6097bdecb770e455c5c1ee0a600
Successfully built astraforge-backend
Installing collected packages: websockets, uvloop, typing-extensions, pyyaml, python-dotenv, psycopg-binary, MarkupSafe, idna, httptools, h11, greenlet, click, certifi, annotated-types, annotated-doc, uvicorn, typing-inspection, sqlalchemy, pydantic-core, psycopg, Mako, httpcore, anyio, watchfiles, starlette, pydantic, httpx, alembic, pydantic-settings, fastapi, astraforge-backend

Successfully installed Mako-1.3.12 MarkupSafe-3.0.3 alembic-1.18.5 annotated-doc-0.0.4 annotated-types-0.7.0 anyio-4.14.2 astraforge-backend-0.4.0 certifi-2026.7.22 click-8.4.2 fastapi-0.139.2 greenlet-3.5.4 h11-0.16.0 httpcore-1.0.9 httptools-0.8.0 httpx-0.28.1 idna-3.18 psycopg-3.3.4 psycopg-binary-3.3.4 pydantic-2.13.4 pydantic-core-2.46.4 pydantic-settings-2.14.2 python-dotenv-1.2.2 pyyaml-6.0.3 sqlalchemy-2.0.51 starlette-1.3.1 typing-extensions-4.16.0 typing-inspection-0.4.2 uvicorn-0.51.0 uvloop-0.22.1 watchfiles-1.2.0 websockets-16.1.1
 ---> Removed intermediate container 1f8471796611
 ---> 6d5662ea6263
Step 10/12 : USER astrforge
 ---> Running in 502a5f7f3f55
 ---> Removed intermediate container 502a5f7f3f55
 ---> a02935fbcc51
Step 11/12 : EXPOSE 8000
 ---> Running in e54626f5aca5
 ---> Removed intermediate container e54626f5aca5
 ---> 68f9765dffb2
Step 12/12 : CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
 ---> Running in b7273bedcf4a
 ---> Removed intermediate container b7273bedcf4a
 ---> 60a2b901e7b3
Successfully built 60a2b901e7b3
Successfully tagged test-build:latest
```

## Runtime Verification inside Built Container

We verified the container successfully runs, can call `uvicorn --version`, and can import the Python application structure with zero errors:

```console
$ docker run --rm test-build uvicorn --version
Running uvicorn 0.51.0 with CPython 3.12.13 on Linux

$ docker run --rm test-build python -c "import app"
# Completed successfully without any output (exit status 0)
```

This completes verification for BE-24.
