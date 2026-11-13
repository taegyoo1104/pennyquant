FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.lock ./
RUN python -m pip install -r requirements.lock
COPY pyproject.toml README.md ./
COPY pennyquant ./pennyquant
COPY .streamlit ./.streamlit
RUN python -m pip install --no-deps . && useradd --uid 1000 --create-home pq && mkdir -p /app/data && chown -R pq:pq /app
USER pq
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health',timeout=3)"
CMD ["streamlit", "run", "pennyquant/dashboard/app.py", "--server.address=0.0.0.0"]
