FROM python:3.11-slim

WORKDIR /app

COPY eval_dashboard/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY eval_dashboard /app/eval_dashboard
COPY evals /app/evals

ENV PYTHONPATH=/app
EXPOSE 8080

CMD ["python", "eval_dashboard/app.py"]
