"""
Backend Vicky - Sistema de Gestión de Leads con WhatsApp Cloud API
Versión Corregida para Producción en Render.com
Autor: Soluciones Financieras MX
"""

import os
import json
import logging
import re
from datetime import datetime
from urllib.parse import urlparse
from typing import List, Tuple, Optional

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ================= CONFIGURACIÓN INICIAL =================
# Configuración de logging para Render (estructurado)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ================= CONFIGURACIÓN DE SEGURIDAD =================
# Sanitización robusta de ALLOWED_ORIGINS
def get_allowed_origins() -> List[str]:
    """Obtiene y sanitiza lista de orígenes permitidos"""
    origins_raw = os.getenv('ALLOWED_ORIGINS', '')
    
    if not origins_raw:
        logger.warning("ALLOWED_ORIGINS no configurado")
        return []
    
    # Split, strip espacios, filtrar vacíos y validar URLs
    origins = []
    for origin in origins_raw.split(','):
        origin_clean = origin.strip()
        if origin_clean:
            # Validar formato básico de URL
            if origin_clean.startswith(('http://', 'https://')):
                origins.append(origin_clean)
            else:
                logger.warning(f"Origen con formato inválido omitido: {origin_clean}")
    
    logger.info(f"Orígenes permitidos configurados: {origins}")
    return origins

# Variables de entorno
ALLOWED_ORIGINS = get_allowed_origins()
WHATSAPP_API_TOKEN = os.getenv('WHATSAPP_API_TOKEN', '')
WHATSAPP_PHONE_ID = os.getenv('WHATSAPP_PHONE_ID', '')
WHATSAPP_API_VERSION = os.getenv('WHATSAPP_API_VERSION', 'v17.0')
WHATSAPP_API_URL = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_ID}/messages"
ADMIN_PHONE = os.getenv('ADMIN_PHONE', '')
REDIS_URL = os.getenv('REDIS_URL', '')
GOOGLE_SHEETS_ENABLED = os.getenv('GOOGLE_SHEETS_ENABLED', 'false').lower() == 'true'

# ================= CONFIGURACIÓN RATE LIMITING =================
# Configuración escalable: Redis si está disponible, sino memory
if REDIS_URL:
    logger.info(f"Configurando rate limiting con Redis: {REDIS_URL}")
    rate_limit_storage = REDIS_URL
else:
    logger.info("Configurando rate limiting en memoria (para single worker)")
    rate_limit_storage = "memory://"

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["100 per hour"],
    storage_uri=rate_limit_storage,
    strategy="fixed-window"
)

# ================= CONFIGURACIÓN CORS =================
def origin_is_allowed(origin: str) -> bool:
    """
    Valida que el Origin esté en la lista permitida
    Maneja requests sin Origin (curl/Postman) para desarrollo
    """
    if not origin:
        # Permitir requests sin Origin solo si es ambiente de desarrollo
        return os.getenv('FLASK_ENV') == 'development'
    
    try:
        domain = urlparse(origin).netloc
    except Exception:
        logger.warning(f"Error parseando origen: {origin}")
        return False
    
    # Verificar contra lista de permitidos
    allowed_domains = []
    for allowed in ALLOWED_ORIGINS:
        try:
            allowed_domain = urlparse(allowed).netloc
            allowed_domains.append(allowed_domain)
        except Exception:
            continue
    
    is_allowed = domain in allowed_domains
    
    if not is_allowed:
        logger.warning(f"Origen no permitido: {origin} (dominio: {domain})")
        logger.info(f"Dominios permitidos: {allowed_domains}")
    
    return is_allowed

# Configurar CORS con validación dinámica
CORS(app, origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS else [], 
     supports_credentials=True,
     allow_headers=['Content-Type', 'Authorization'])

# ================= FUNCIONES DE VALIDACIÓN =================
def validate_payload(data: dict) -> Tuple[bool, str]:
    """Valida estructura del payload del lead"""
    required_fields = ['name', 'phone', 'service']
    
    if not isinstance(data, dict):
        return False, "Payload debe ser objeto JSON"
    
    if 'lead' not in data:
        return False, "Falta objeto 'lead'"
    
    lead = data['lead']
    
    # Validar campos requeridos
    for field in required_fields:
        if field not in lead or not lead[field]:
            return False, f"Campo requerido faltante: {field}"
    
    # Validar teléfono mexicano (10 dígitos)
    phone = re.sub(r'\D', '', str(lead['phone']))
    if len(phone) != 10:
        return False, "Teléfono debe tener 10 dígitos"
    
    # Validar servicio permitido
    allowed_services = [
        'Seguro Auto', 'Seguro Vida', 'Seguro Hogar',
        'Crédito Personal', 'Crédito Hipotecario',
        'Terminal Bancaria', 'Otro'
    ]
    
    if lead['service'] not in allowed_services:
        return False, f"Servicio no permitido: {lead['service']}"
    
    return True, phone  # Retorna phone normalizado

def normalize_phone(phone: str) -> str:
    """Normaliza teléfono a formato E.164 para WhatsApp"""
    # Remover todo excepto números
    clean_phone = re.sub(r'\D', '', str(phone))
    
    # Si tiene 10 dígitos, asumir México (+52)
    if len(clean_phone) == 10:
        return f"52{clean_phone}"
    
    # Si ya tiene código de país, dejarlo
    return clean_phone

# ================= FUNCIONES WHATSAPP =================
def send_whatsapp_template(phone: str, name: str, service: str) -> bool:
    """Envía plantilla de WhatsApp Cloud API al cliente"""
    if not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_ID:
        logger.warning("WhatsApp API no configurada para templates")
        return False
    
    normalized_phone = normalize_phone(phone)
    
    payload = {
        "messaging_product": "whatsapp",
        "to": normalized_phone,
        "type": "template",
        "template": {
            "name": "lead_bienvenida",
            "language": {
                "code": "es_MX"
            },
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": name},
                        {"type": "text", "text": service}
                    ]
                }
            ]
        }
    }
    
    headers = {
        "Authorization": f"Bearer {WHATSAPP_API_TOKEN}",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(
            WHATSAPP_API_URL,
            headers=headers,
            json=payload,
            timeout=10
        )
        
        if response.status_code == 200:
            logger.info(f"Template WhatsApp enviado a {normalized_phone}")
            return True
        else:
            logger.error(f"Error WhatsApp template: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"Excepción WhatsApp template: {e}")
        return False

def send_whatsapp_text(phone: str, text: str) -> bool:
    """Envía mensaje de texto simple por WhatsApp"""
    if not WHATSAPP_API_TOKEN or not WHATSAPP_PHONE_ID:
        logger.warning("WhatsApp API no configurada para textos")
        return False
    
    normalized_phone = normalize_phone(phone)
    
    payload = {
        "messaging_product": "whatsapp",
        "to": normalized_phone,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": text
        }
    }
    
    headers = {
        "Authorization": f"Bearer {WHATSAPP_API_TOKEN}",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(
            WHATSAPP_API_URL,
            headers=headers,
            json=payload,
            timeout=10
        )
        
        if response.status_code == 200:
            logger.info(f"Texto WhatsApp enviado a {normalized_phone}")
            return True
        else:
            logger.error(f"Error WhatsApp texto: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"Excepción WhatsApp texto: {e}")
        return False

def notify_advisor(lead_data: dict) -> bool:
    """Notifica al asesor vía WhatsApp con mensaje de texto"""
    if not ADMIN_PHONE:
        logger.warning("ADMIN_PHONE no configurado, omitiendo notificación")
        return False
    
    message = (
        f"🚨 NUEVO LEAD RECIBIDO\n"
        f"Nombre: {lead_data['name']}\n"
        f"Teléfono: {lead_data['phone']}\n"
        f"Servicio: {lead_data['service']}\n"
        f"Email: {lead_data.get('email', 'No proporcionado')}\n"
        f"Hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"IP: {lead_data.get('metadata', {}).get('ip', 'N/A')}"
    )
    
    return send_whatsapp_text(ADMIN_PHONE, message)

# ================= PERSISTENCIA DE LEADS =================
def save_to_google_sheets(lead_data: dict) -> bool:
    """Guarda lead en Google Sheets (solo si está configurado)"""
    if not GOOGLE_SHEETS_ENABLED:
        return False
    
    try:
        # TODO: Implementar Google Sheets API
        # Por ahora solo log para desarrollo
        logger.info(f"Lead para Google Sheets: {json.dumps(lead_data, ensure_ascii=False)}")
        return True
    except Exception as e:
        logger.error(f"Error Google Sheets: {e}")
        return False

def persist_lead(lead_data: dict) -> bool:
    """
    Persiste lead de manera segura para Render
    - Log estructurado a stdout (capturado por Render logs)
    - Google Sheets si está habilitado
    - NO usa archivos locales
    """
    try:
        # 1. Log estructurado (siempre disponible)
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "type": "lead_received",
            "data": lead_data,
            "source": "web_form"
        }
        logger.info(json.dumps(log_entry, ensure_ascii=False))
        
        # 2. Google Sheets (opcional)
        sheets_success = save_to_google_sheets(lead_data)
        if sheets_success:
            logger.info("Lead guardado en Google Sheets")
        
        return True
        
    except Exception as e:
        logger.error(f"Error persistiendo lead: {e}")
        # No fallar el flujo completo por error de persistencia
        return False

# ================= ENDPOINTS =================
@app.route('/api/health', methods=['GET'])
def health_check():
    """Endpoint de salud para Render"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "vicky-lead-manager",
        "version": "2.0",
        "features": {
            "whatsapp_configured": bool(WHATSAPP_API_TOKEN and WHATSAPP_PHONE_ID),
            "google_sheets": GOOGLE_SHEETS_ENABLED,
            "rate_limit_storage": "redis" if REDIS_URL else "memory",
            "allowed_origins_count": len(ALLOWED_ORIGINS)
        }
    }), 200

@app.route('/api/web-lead', methods=['POST', 'OPTIONS'])
@limiter.limit("10 per minute")  # Rate limit específico
def web_lead():
    """
    Endpoint principal para recepción de leads
    """
    
    # Manejar preflight CORS
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    
    # 1. Validar Origin (permitir sin origin en desarrollo)
    origin = request.headers.get('Origin')
    if not origin_is_allowed(origin):
        error_msg = "Origen no autorizado" if origin else "Origin header requerido"
        logger.warning(f"{error_msg}: {origin}")
        return jsonify({"error": error_msg}), 403
    
    # 2. Validar Content-Type
    if not request.is_json:
        logger.warning("Content-Type no es application/json")
        return jsonify({"error": "Content-Type debe ser application/json"}), 415
    
    # 3. Parsear JSON
    try:
        data = request.get_json()
    except Exception as e:
        logger.warning(f"JSON malformado: {e}")
        return jsonify({"error": "JSON malformado"}), 400
    
    # 4. Validar estructura del payload
    is_valid, validation_result = validate_payload(data)
    if not is_valid:
        logger.warning(f"Payload inválido: {validation_result}")
        return jsonify({"error": validation_result}), 400
    
    # 5. Preparar datos del lead
    lead = data['lead']
    normalized_phone = validation_result  # Phone ya validado
    
    lead_data = {
        "name": lead['name'].strip(),
        "phone": normalized_phone,
        "email": lead.get('email', '').strip(),
        "service": lead['service'],
        "notes": lead.get('notes', ''),
        "tags": lead.get('tags', []),
        "metadata": {
            "timestamp": datetime.now().isoformat(),
            "ip": request.remote_addr,
            "user_agent": request.headers.get('User-Agent', ''),
            "received_via": "web_form",
            "origin": origin
        }
    }
    
    # 6. Persistir lead (no bloqueante)
    persist_success = persist_lead(lead_data)
    if not persist_success:
        logger.error("Error en persistencia, continuando con flujo...")
    
    # 7. Enviar WhatsApp al cliente
    whatsapp_sent = False
    try:
        whatsapp_sent = send_whatsapp_template(
            normalized_phone,
            lead_data['name'],
            lead_data['service']
        )
    except Exception as e:
        logger.error(f"Error enviando WhatsApp al cliente: {e}")
    
    # 8. Notificar al asesor (solo si WhatsApp al cliente se envió)
    advisor_notified = False
    if whatsapp_sent:
        try:
            advisor_notified = notify_advisor(lead_data)
        except Exception as e:
            logger.error(f"Error notificando asesor: {e}")
    
    # 9. Responder al frontend
    response_data = {
        "ok": True,
        "lead_id": f"lead_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "whatsapp_sent": whatsapp_sent,
        "advisor_notified": advisor_notified,
        "persisted": persist_success,
        "message": "Lead recibido correctamente",
        "next_step": "Revisa WhatsApp para continuar" if whatsapp_sent else "Contacta directamente por WhatsApp"
    }
    
    logger.info(f"Lead procesado exitosamente: {lead_data['name']} - WhatsApp: {whatsapp_sent}")
    return jsonify(response_data), 201

# ================= MANEJO DE ERRORES =================
@app.errorhandler(429)
def ratelimit_handler(e):
    """Manejo de rate limit excedido"""
    logger.warning(f"Rate limit excedido: {request.remote_addr}")
    return jsonify({
        "error": "Demasiadas solicitudes",
        "message": "Por favor intenta de nuevo en unos minutos"
    }), 429

@app.errorhandler(404)
def not_found_handler(e):
    """Manejo de rutas no encontradas"""
    logger.warning(f"Ruta no encontrada: {request.path}")
    return jsonify({"error": "Ruta no encontrada"}), 404

@app.errorhandler(500)
def internal_error_handler(e):
    """Manejo de errores internos"""
    logger.error(f"Error interno en {request.path}: {e}")
    return jsonify({"error": "Error interno del servidor"}), 500

# ================= EJECUCIÓN =================
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    
    # Solo ejecutar en desarrollo (Render usa gunicorn)
    if os.getenv('RENDER', '').lower() != 'true':
        app.run(host='0.0.0.0', port=port, debug=False)
