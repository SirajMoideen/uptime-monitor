FROM python:3.11-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates/ templates/

# Expose the Flask port
EXPOSE 5000
ENV PYTHONUNBUFFERED=1

CMD ["python", "app.py"]
