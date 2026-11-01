FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml constraints.txt README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir -c constraints.txt . && useradd --create-home pulse
USER pulse
EXPOSE 8010
ENTRYPOINT ["productpulse"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8010"]
