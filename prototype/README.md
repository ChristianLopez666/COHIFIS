# Prototipo estático COHIFIS

Este directorio contiene un prototipo visual estático (mobile-first) de la primera versión comercial de COHIFIS, con foco en:

- Seguro de Vida Temporal.
- Conversión a WhatsApp/Vicky.

## Archivos

- `index.html`: página de Inicio.
- `seguro-de-vida-temporal.html`: página de producto prioritario.
- `styles.css`: estilos compartidos.

## Cómo abrir localmente

No requiere dependencias ni instalación.

1. Abre la carpeta `prototype/`.
2. Haz doble clic en `index.html` para abrir en tu navegador.
3. Navega entre páginas usando los enlaces del header.

Opcional (servidor estático simple con Python ya disponible en muchos entornos):

```bash
cd prototype
python3 -m http.server 8000
```

Luego abre: `http://localhost:8000`

## Notas

- Los enlaces de WhatsApp son placeholders (`wa.me/0000000000`) con mensajes prellenados diferenciados por bloque CTA para trazabilidad comercial.
- El mensaje “hasta 40% de descuento” se mantiene condicionado a perfil, cobertura, aseguradora y condiciones de contratación.
