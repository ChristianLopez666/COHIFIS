# Pruebas Render — WhatsApp Webhook y Vicky Web

Backend objetivo: `https://cohifis.onrender.com`

## 1) Verificación GET challenge de Meta

```bash
curl -i "https://cohifis.onrender.com/api/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=TU_VERIFY_TOKEN&hub.challenge=12345"
```

Resultado esperado:
- HTTP 200
- Body exacto: `12345`

## 2) POST con payload realista de Meta (inbound WhatsApp)

```bash
curl -i -X POST "https://cohifis.onrender.com/api/whatsapp/webhook" \
  -H "Content-Type: application/json" \
  -d '{
    "object": "whatsapp_business_account",
    "entry": [{
      "id": "WABA_ID",
      "changes": [{
        "field": "messages",
        "value": {
          "messaging_product": "whatsapp",
          "metadata": {"display_phone_number": "5210000000000", "phone_number_id": "123456789"},
          "contacts": [{"profile": {"name": "Cliente Demo"}, "wa_id": "5216680000000"}],
          "messages": [{
            "from": "5216680000000",
            "id": "wamid.HBgL...",
            "timestamp": "1710000000",
            "text": {"body": "Tengo duda del descuento en el seguro de vida"},
            "type": "text"
          }]
        }
      }]
    }]
  }'
```

Resultado esperado:
- HTTP 200
- Body JSON con `{"status":"processed"}`
- El usuario recibe respuesta útil por WhatsApp (no solo acuse de recibido)

## 3) POST sin messages (debe ignorar sin error)

```bash
curl -i -X POST "https://cohifis.onrender.com/api/whatsapp/webhook" \
  -H "Content-Type: application/json" \
  -d '{"entry":[{"changes":[{"value":{"messages":[]}}]}]}'
```

Resultado esperado:
- HTTP 200
- Body JSON con `{"status":"ignored"}`

## 4) Prueba Vicky Web contra backend real

```bash
curl -i -X POST "https://cohifis.onrender.com/api/v1/vicky-chat" \
  -H "Origin: https://cohifis-web.onrender.com" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Tengo duda del descuento en el seguro de vida",
    "context": {
      "source_channel": "web",
      "source": "vicky_web_widget",
      "page_url": "https://cohifis.com.mx/seguro-de-vida-temporal.html",
      "product_context": "seguro_de_vida_temporal",
      "conversation_id": "test-render-vicky-web-001"
    }
  }'
```

Resultado esperado:
- HTTP 200
- JSON con `answer` no vacío
- Sin error 403 por política de origen

## 5) Validar CORS (preflight)

```bash
curl -i -X OPTIONS "https://cohifis.onrender.com/api/v1/vicky-chat" \
  -H "Origin: https://cohifis-web.onrender.com" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type"
```

Resultado esperado:
- HTTP 200
- Encabezado `access-control-allow-origin: https://cohifis-web.onrender.com`
