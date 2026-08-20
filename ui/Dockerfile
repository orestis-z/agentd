FROM python:3.12-slim
RUN pip install --no-cache-dir flask croniter pyyaml
WORKDIR /app
COPY . .
RUN chmod -R a+rX /app
EXPOSE 5000
CMD ["python", "app.py"]
