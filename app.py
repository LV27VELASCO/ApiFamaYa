from datetime import timedelta
import os
import uuid
from dotenv import load_dotenv
from flask import Flask, make_response, request, jsonify, json
from flask_cors import CORS
from flask_jwt_extended import JWTManager, create_access_token, get_jwt_identity, jwt_required
from pydantic import ValidationError
import requests
import stripe
from google import genai
from google.genai import types
from supabase import create_client, Client
from models import Response, Items, ServicesCheckOut
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


load_dotenv()

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["100 per hour"]  # límite global: 100 peticiones/hora por IP
)

# Configura la clave secreta
app.config['JWT_TOKEN_LOCATION'] = ['headers']
app.config['JWT_SECRET_KEY'] = os.environ.get("SECRET_JWT")
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=2)

jwt = JWTManager(app)
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
client = genai.Client(api_key=os.environ.get("GEMINI_KEY"))

# Habilitar CORS para todas las rutas
# Configuración de CORS
CORS(app, origins=["http://localhost:4200", "http://127.0.0.1:5500", "https://fama-ya.vercel.app"], 
     supports_credentials=True, 
     allow_headers=["Content-Type", "Authorization"], 
     methods=["GET","POST"])

#FOLLOWERS, LIKES, VIEWS
CODE_SERVICE = {
            "instagram": ["5712","4365","556"],
            "facebook": ["1636","1101","9598"],
            "tiktok": ["8521","2079","6990"]
        }

@app.route('/api/token', methods=['GET'])
@limiter.limit("10 per minute")
def generar_token():
    session_id = str(uuid.uuid4())  # ID aleatorio de sesión
    access_token = create_access_token(identity=session_id)
    return jsonify(Response(message=access_token).model_dump()),200


@app.route('/api/services/<slug>', methods=['GET'])
@jwt_required(locations=['headers'])
def services(slug):
    identidad = get_jwt_identity()
    statusCode = 200
    try:
        response = supabase.table("services")\
            .select("slug, id, prices(id,quantity,bonus,price)")\
            .eq("slug", slug)\
            .execute()
        if not response.data:
            return jsonify(Response(message='Servicio no encontrado').model_dump()), 404

        prices_services = json.loads(response.model_dump_json())
        return jsonify(prices_services['data'][0]), statusCode
    except ValidationError as e:
        statusCode = 400
        response = Response(message=str(e))
        return jsonify(response.model_dump()), statusCode
    except Exception as e:
        statusCode = 500
        response = Response(message=str(e))
        return jsonify(response.model_dump()), statusCode
    

@app.route('/api/all-services', methods=['GET'])
@jwt_required(locations=['headers'])
def allservices():
    statusCode = 200
    try:
        response = supabase.table("services")\
        .select("id_service, slug, prices(id_price, quantity, bonus, price)")\
        .execute()
        for service in response.data:
            service['prices'] = sorted(service['prices'], key=lambda x: x['quantity'])
        if not response.data:
            return jsonify(Response(message='Servicio no encontrado').model_dump()), 404

        prices_services = json.loads(response.model_dump_json())
        return jsonify(prices_services['data']), statusCode
    except ValidationError as e:
        statusCode = 400
        response = Response(message=str(e))
        return jsonify(response.model_dump()), statusCode
    except Exception as e:
        statusCode = 500
        response = Response(message=str(e))
        return jsonify(response.model_dump()), statusCode
    
@app.route('/api/get-orders', methods=['GET'])
@jwt_required(locations=['headers'])
def get_orders():
    statusCode = 200
    session_id = request.args.get('session_id')
    if not session_id:
        return jsonify(Response(message='Session_id requerido').model_dump()), 400

    try:
        response = supabase.table("orders_success")\
            .select("order")\
            .eq("session_id", session_id)\
            .execute()
        
        if not response.data:
            return jsonify(Response(message='Orden no encontrada').model_dump()), 404
        orders_data = [row["order"] for row in response.data]
        return jsonify(orders_data[0]), statusCode
    except ValidationError as e:
        statusCode = 400
        response = Response(message=str(e))
        return jsonify(response.model_dump()), statusCode
    except Exception as e:
        statusCode = 500
        response = Response(message=str(e))
        return jsonify(response.model_dump()), statusCode

@app.route('/api/checkout-session', methods=['POST'])
#@jwt_required(locations=['headers'])
def create_checkout_session():
    data = Items.model_validate(request.json) # Suponiendo que recibes items desde el frontend
    
    try:
        products =[]
        for item in data.items:
           product = validate_services(item.slug, item.id,item.url)
           if product:
                products.append(product[0])

        payload=[]
        for item in products:
            transformed = {
                "price": item["price"],
                "quantity": item["quantity"] + item["bonus"],
                "slug": item["service"]["slug"],
                "url": item["url"]
            }
            payload.append(transformed)

        stripe.api_key = os.environ.get("SECRET_KEY")
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='payment',
            line_items=[
                {
                    'price_data': {
                        'currency': 'eur',
                        'product_data': {
                            'name':  f"{item['quantity'] + item['bonus']:,}".replace(",", ".") + ' ' +  item['service']['name'],
                        },
                        'unit_amount': int(item['price'] * 100),  # En centavos
                    },
                    'quantity': 1,
                } for item in products
            ],
            metadata={"orders": json.dumps(payload)},
            success_url='http://localhost:4200/success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url='https://tuweb.com/cancel',
        )
        return jsonify(session), 200
    except Exception as e:
        return jsonify(error=str(e)), 400


@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get('stripe-signature')
    webhook_secret = os.environ.get('SECRET_WEBHOOK')
    event = None
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
    except stripe.error.SignatureVerificationError as e:
        return f'Webhook error: {str(e)}', 400

    # Verificamos que sea un pago completado
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        session_id = session.get('id')
        
        # Recuperar carrito desde metadata
        orders = json.loads(session["metadata"]["orders"])
        if orders:
            # Procesar cada ítem del carrito
            orders_data=[]
            for order in orders:
                slug = order.get('slug')
                url = order.get('url')
                quantity = order.get('quantity')
                price = order.get('price')
                # Aquí llamas a tu API interna de entrega
                order_id = entregar_producto(slug, url, quantity)
                orders_data.append({ "slug": slug, "url": url, "quantity": quantity,"price": price ,"order_id": order_id})
            
            insert_data(session_id,orders_data)
    return 'OK', 200

def entregar_producto(slug, url, cantidad):
    order_id = None  # evitar UnboundLocalError

    if any(x in slug for x in ["instagram-followers", "instagram-likes", "instagram-views"]):
        order_id = service_instagram(slug, url, cantidad)
    elif any(x in slug for x in ["tiktok-followers", "tiktok-likes", "tiktok-views"]):
        order_id = service_tiktok(slug, url, cantidad)
    elif any(x in slug for x in ["facebook-followers", "facebook-likes", "facebook-views"]):
        order_id = service_facebook(slug, url, cantidad)

    return order_id

def service_instagram(slug, url, cantidad):
    order_id:str
    if "instagram-followers" in slug:
         order_id = send_order(CODE_SERVICE["instagram"][0], url, cantidad)
    elif "instagram-likes" in slug:
         order_id = send_order(CODE_SERVICE["instagram"][1], url, cantidad)
    elif "instagram-views" in slug:
         order_id = send_order(CODE_SERVICE["instagram"][2], url, cantidad)
    return order_id

def service_tiktok(slug, url, cantidad):
    order_id:str
    if "tiktok-followers" in slug:
         order_id = send_order(CODE_SERVICE["tiktok"][0], url, cantidad)
    elif "tiktok-likes" in slug:
         order_id = send_order(CODE_SERVICE["tiktok"][1], url, cantidad)
    elif "tiktok-views" in slug:
         order_id = send_order(CODE_SERVICE["tiktok"][2], url, cantidad)
    return order_id

def service_facebook(slug, url, cantidad):
    order_id:str
    if "facebook-followers" in slug:
         order_id = send_order(CODE_SERVICE["facebook"][0], url, cantidad)
    elif "facebook-likes" in slug:
         order_id = send_order(CODE_SERVICE["facebook"][1], url, cantidad)
    elif "facebook-views" in slug:
         order_id = send_order(CODE_SERVICE["facebook"][2], url, cantidad)
    return order_id

def send_order(code_service:str, link:str, quantity:str):
    JUSTANOTHER_URL = os.environ.get("JUSTANOTHER_URL")
    JUSTANOTHER_KEY = os.environ.get("JUSTANOTHER_KEY")
    try:
        payload = {
            "key": JUSTANOTHER_KEY,    # Reemplaza con tu clave real
            "action": "add",
            "service": code_service,
            "link": link,
            "quantity": quantity
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }

        response = requests.post(JUSTANOTHER_URL,data=payload, headers=headers)
        response.raise_for_status()  # lanza error si status no es 2xx
        data = response.json()
        order_id = data.get("order")
        return order_id
    except requests.exceptions.RequestException as e:
        return ""

def consult_order(code_order:str):
    JUSTANOTHER_URL = os.environ.get("JUSTANOTHER_URL")
    JUSTANOTHER_KEY = os.environ.get("JUSTANOTHER_KEY")
    try:
        payload = {
            "key": JUSTANOTHER_KEY,    # Reemplaza con tu clave real
            "action": "status",
            "order": code_order
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }

        response = requests.post(JUSTANOTHER_URL,data=payload, headers=headers)
        response.raise_for_status()  # lanza error si status no es 2xx
        data = response.json()
        print("estado_orden",data)
        order_id = data.get("order")
        return order_id
    except requests.exceptions.RequestException as e:
        return ""

def validate_services(slug:str, id_price:str, url:str):
    consult = supabase.table("prices")\
    .select("id_price, quantity, bonus, price, service:services(id_service, name, slug)")\
    .eq("id_price", id_price.strip())\
    .eq("service.slug", slug.strip())\
    .execute()
    response = [item for item in consult.data if item["service"] is not None]

    if response:
        for item in response:
            item["url"] = url

    return response

def insert_data(session_id:str, order:str):
    response = (supabase.table("orders_success").insert([{"session_id": session_id, "order": order}]).execute())

if __name__ == "__main__":
    print("Servidor iniciado en http://localhost:2000")
    app.run(debug=True, port=2000)