# Fahrplan-Dienst. Python nur mit Standardbibliothek, keine Pakete.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app
COPY fahrplan_dienst/ /app/fahrplan_dienst/

# Eigener Systembenutzer ohne Home. Der Dienst schreibt nichts auf die Platte.
RUN useradd --system --uid 10003 --user-group --no-create-home --shell /usr/sbin/nologin fahrplan

USER fahrplan
EXPOSE 8080

CMD ["python", "-m", "fahrplan_dienst"]
