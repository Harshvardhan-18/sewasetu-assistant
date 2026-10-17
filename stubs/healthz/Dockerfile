FROM python:3.11-slim
WORKDIR /stub
COPY server.py .
COPY assets ./assets
EXPOSE 8080
CMD ["python", "server.py", "8080"]
