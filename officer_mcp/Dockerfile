FROM python:3.11-slim

WORKDIR /app
COPY officer_mcp/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_common/ /app/mcp_common/
COPY officer_mcp/ /app/officer_mcp/

WORKDIR /app/officer_mcp
EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
