import sys
import io
if sys.stdout is None:
    sys.stdout = sys.stderr = io.StringIO()
import os
import webbrowser
from threading import Timer
import datetime
import re
import calendar
from collections import defaultdict
from functools import wraps
from urllib.parse import quote
import math
import traceback
from sqlalchemy import desc
# 从Flask框架导入核心功能，用于构建Web应用。
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, session, jsonify, Response, send_from_directory, make_response)
# 从Werkzeug库导入工具，用于安全地处理文件名。
from werkzeug.utils import secure_filename
# 从Pillow库导入图像处理功能。
from PIL import Image
# 从SQLAlchemy库导入功能，用于数据库查询排序。
from sqlalchemy import desc
# 从Flask-SocketIO库导入核心类，用于实现WebSocket实时通信。
from flask_socketio import SocketIO, emit 

# 导入本地模块。
import config  # 应用程序的配置文件
import helpers # 自定义的辅助函数模块
from models import db, User, Patient, XRay, Appointment, Prescription, ServiceItem,Invoice, InvoiceItem,Payment,bcrypt # 数据库模型

# 应用程序初始化区域
# 创建和配置核心应用对象。
# 创建Flask应用的核心对象，这是整个Web应用的起点。
app = Flask(__name__)
# 从 `config.py` 文件加载所有大写变量作为应用的配置。
app.config.from_object('config')

# 初始化数据库ORM（对象关系映射）工具，将其与Flask应用关联。
db.init_app(app)
# 初始化密码哈希工具，用于安全地处理用户密码。
bcrypt.init_app(app)

# 初始化SocketIO，用于实现浏览器、服务器、硬件设备之间的实时双向通信。
# async_mode=None: 自动选择最佳的异步网络库（如eventlet）。
# cors_allowed_origins="*": 允许来自任何来源的WebSocket连接，这对于ESP32等硬件客户端是必需的。
socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins="*")

# 创建一个全局集合（set），用于存储所有在线硬件设备（如ESP32-CAM）的唯一会话ID。
# 使用集合可以自动处理重复值，并提供高效的成员资格测试。
hardware_clients = set()

# 在Flask应用上下文中执行初始化操作。
# 这确保了在应用运行前，数据库和AI模型都已准备就绪。
with app.app_context():
    # 初始化数据库（如果不存在，则创建表）。
    helpers.init_database(app)
    # 加载AI模型到内存中，以提高后续推理的速度。
    helpers.load_models()

#               Web应用装饰器定义区域
# 装饰器是Python的一种高级功能，可以为现有函数添加额外的行为。
def login_required(f):
    """
    一个自定义的装饰器，用于保护需要用户登录才能访问的路由（页面）。
    如果用户会话（session）中不存在'user'键，则认为用户未登录，
    会闪现一条提示消息并将其重定向到登录页面。
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            flash('请先登录以访问此页面。', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def role_required(required_role):
    """
    一个更严格的装饰器，用于实现基于角色的访问控制（RBAC）。
    它接收一个必需的角色作为参数（如'admin'），并确保只有拥有该角色的用户
    才能访问被此装饰器保护的路由。
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if session.get('user', {}).get('role') != required_role:
                flash(f'您没有权限访问此页面，需要 "{required_role}" 角色。', 'danger')
                return redirect(url_for('index'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

#                  Web应用上下文处理器
@app.context_processor
def inject_helpers():
    """
    将 `helpers` 模块注入到所有Jinja2模板的上下文中。
    这允许我们在HTML模板中直接调用 `helpers` 模块里的函数，
    例如 `{{ helpers.some_function() }}`，增强了模板的灵活性。
    """
    return dict(helpers=helpers)

#          远程硬件控制核心功能 (ESP32-CAM 交互)
@app.route('/controller')
@login_required
def controller():
    """渲染远程硬件控制器的Web界面 (`controller.html`)。"""
    return render_template('controller.html')

# --- WebSocket 事件处理函数 ---

def update_hardware_status():
    """
    一个辅助函数，用于计算当前硬件在线数量，并构建状态字典，
    然后通过WebSocket的 `hardware_status_update` 事件广播给所有网页客户端。
    """
    status = {
        'count': len(hardware_clients),
        'is_connected': len(hardware_clients) > 0
    }
    socketio.emit('hardware_status_update', status)
    print(f"广播硬件状态: {status}")

@socketio.on('connect')
def handle_connect(auth=None):
    """处理所有新客户端（包括浏览器和硬件）的通用连接事件。"""
    print(f'客户端已连接: {request.sid}')
    # 当有任何新客户端连接时，都向其广播一次当前的硬件状态，以确保其UI正确显示。
    update_hardware_status()

@socketio.on('disconnect')
def handle_disconnect():
    """处理客户端断开连接的事件，如果断开的是硬件，则更新在线设备列表。"""
    print(f'客户端已断开: {request.sid}')
    # 检查断开的客户端ID是否在我们已登记的硬件列表中。
    if request.sid in hardware_clients:
        hardware_clients.remove(request.sid)
        print(f"硬件设备 {request.sid} 已下线。")
        # 广播更新后的硬件状态。
        update_hardware_status()

@socketio.on('hardware_hello')
def handle_hardware_hello(data=None):
    """处理来自ESP32的特定“报到”事件，将其ID注册为硬件设备。"""
    print(f"收到硬件设备的报到信号: {request.sid}")
    # 将该设备的会话ID添加到我们的硬件集合中进行追踪。
    hardware_clients.add(request.sid)
    # 广播更新后的硬件状态。
    update_hardware_status()

@socketio.on('trigger_photo_command')
def handle_trigger_photo_command(data):
    """接收来自网页的拍照指令，并将其广播给所有已注册的硬件设备。"""
    if not hardware_clients:
        print("警告: 收到拍照指令，但当前没有硬件设备在线。")
        # 向发送指令的网页客户端返回一个错误提示。
        emit('scan_error', {'message': '指令已发送，但没有扫描仪设备在线。'})
        return
    
    print(f"收到来自网页的拍照指令，正在转发给 {len(hardware_clients)} 个硬件设备...")
    # 使用 'server_command' 事件名，将拍照指令广播给所有客户端（硬件会监听此事件）。
    emit('server_command', {'command': 'take_photo'}, broadcast=True)

@socketio.on('set_scan_patient')
def handle_set_scan_patient(data):
    """当Web客户端发起扫描前，设置当前扫描会话对应的患者ID。"""
    patient_id = data.get('patient_id')
    if patient_id:
        session['current_scan_patient_id'] = patient_id
        print(f"扫描会话已设置为患者ID: {patient_id}")

@app.route('/api/upload', methods=['POST'])
def api_upload():
    """
    【已修正】接收ESP32上传的图片，执行AI分析，并广播实际可用的信息。
    """
    try:
        image_data = request.data
        if not image_data:
            return jsonify({'status': 'error', 'message': 'No image data received'}), 400
        
        original_filename = f"esp32_upload_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
        original_filepath = os.path.join(app.config['UPLOAD_FOLDER'], original_filename)
        
        # 1. 保存原图
        with open(original_filepath, 'wb') as f:
            f.write(image_data)
        print(f"图片已接收并保存至: {original_filepath}")

        # 2. 进行AI分析并生成带标注的覆盖图
        ai_results = helpers.run_yolo_inference(original_filepath)
        print(f"AI分析完成，原始结果: {ai_results}")
        
        img_original = Image.open(original_filepath)
        img_with_overlays = helpers.draw_overlays_on_image(img_original, ai_results)
        
        base, _ = os.path.splitext(original_filename)
        overlay_filename = f"overlay_{base}.png"
        overlay_filepath = os.path.join(app.config['UPLOAD_FOLDER'], overlay_filename)
        img_with_overlays.save(overlay_filepath)
        
        # 3. 将分析结果格式化为文本
        result_text = helpers.format_ai_results_for_display(ai_results)
        
        #              【关键修正】保存X光片记录到数据库
        # ===============================================================
        patient_id_for_scan = session.get('current_scan_patient_id')
        if patient_id_for_scan:
            print(f"检测到扫描会话，正在为患者ID {patient_id_for_scan} 保存X光片记录...")
            xray_data = {
                'filename': original_filename,
                'upload_date': datetime.datetime.now(),
                'ai_results': ai_results
            }
            # 调用我们已有的辅助函数来保存
            helpers.add_xray_to_patient(patient_id_for_scan, xray_data)
            print("X光片记录保存成功。")
        else:
            print("警告：未找到扫描会话对应的患者ID，此X光片记录未存入数据库。")
        # ===============================================================

        # 通过WebSocket广播通知前端（这部分逻辑不变）
        socketio.emit('analysis_complete', {
            'image_url': url_for('serve_uploaded_file', filename=original_filename),
            'overlay_url': url_for('serve_uploaded_file', filename=overlay_filename),
            'analysis_result': result_text
        })

        return jsonify({ 'status': 'success' })

    except Exception as e:
        # ... 错误处理 ...
        print(f"处理上传的图片时发生严重错误: {e}")
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': 'Internal Server Error'}), 500

#                   核心业务逻辑路由区域
#     (此区域定义了应用的主要页面和功能，如登录、仪表盘、病患管理等)
@app.route('/')
def index():
    """应用程序的根路由，作为访问入口。它会根据用户的登录状态和角色，自动重定向到相应的页面。"""
    if 'user' not in session:
        return redirect(url_for('login'))
    role = session['user']['role']
    if role == 'admin': return redirect(url_for('admin_dashboard'))
    if role == 'doctor': return redirect(url_for('doctor_dashboard'))
    if role == 'patient': return redirect(url_for('patient_dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    """提供登录页面(GET请求)并处理用户的登录和注册表单提交(POST请求)。"""
    if request.method == 'POST':
        action = request.form.get('action')
        username = request.form.get('username')
        password = request.form.get('password')
        if action == 'login':
            # ... 处理登录 ...
            user = helpers.verify_user(username, password)
            if user:
                session['user'] = {'id': user.id, 'username': user.username, 'role': user.role}
                flash(f'欢迎回来, {user.username}!', 'success')
                return redirect(url_for('index'))
            else:
                flash('用户名或密码错误。', 'danger')
        elif action == 'register':
            # ... 处理注册 ...
            confirm_password = request.form.get('confirm_password')
            role = request.form.get('role')
            if not all([username, password, confirm_password, role]):
                flash('所有带 * 的字段均为必填项。', 'warning')
            elif password != confirm_password:
                flash('两次输入的密码不一致。', 'danger')
            elif role == 'doctor' and request.form.get('registration_code', '') != app.config['DOCTOR_REGISTRATION_CODE']:
                flash('医生专用注册码不正确。', 'danger')
            else:
                success, message = helpers.add_user(username, password, role)
                flash(message, 'success' if success else 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    """清除用户会话，实现登出功能。"""
    session.pop('user', None)
    flash('您已成功登出。', 'info')
    return redirect(url_for('login'))

# --- 管理员功能路由 ---

@app.route('/admin/dashboard')
@login_required
@role_required('admin')
def admin_dashboard():
    """渲染管理员仪表盘，展示所有用户和患者信息，并提供搜索功能。"""
    search_user_query = request.args.get('search_user', '')
    search_patient_query = request.args.get('search_patient', '')
    users_query = User.query
    if search_user_query:
        users_query = users_query.filter(User.username.ilike(f'%{search_user_query}%'))
    patients_query = Patient.query
    if search_patient_query:
        patients_query = patients_query.filter(db.or_(Patient.name.ilike(f'%{search_patient_query}%'), Patient.contact.ilike(f'%{search_patient_query}%')))
    return render_template('admin_dashboard.html', users=users_query.all(), patients=patients_query.all(), search_user_query=search_user_query, search_patient_query=search_patient_query)

@app.route('/admin/user/update', methods=['POST'])
@login_required
@role_required('admin')
def admin_update_user():
    """处理管理员更新用户角色或密码的请求。"""
    username = request.form.get('username')
    new_password = request.form.get('new_password')
    new_role = request.form.get('new_role')
    if helpers.update_user(username, new_password if new_password else None, new_role):
        flash(f'用户 {username} 的信息已成功更新。', 'success')
    else:
        flash(f'更新用户 {username} 失败，用户可能不存在。', 'danger')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/user/delete', methods=['POST'])
@login_required
@role_required('admin')
def admin_delete_user():
    """处理管理员删除用户的请求。"""
    username = request.form.get('username')
    if username == session['user']['username']:
        flash('操作被禁止：不能删除自己的账户！', 'danger')
    elif helpers.delete_user(username):
        flash(f'用户 {username} 及其所有关联数据（病历、预约等）已被成功删除。', 'success')
    else:
        flash(f'删除用户 {username} 失败，用户可能不存在。', 'danger')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/patient/delete', methods=['POST'])
@login_required
@role_required('admin')
def admin_delete_patient():
    """处理管理员删除患者档案的请求。"""
    patient_id = request.form.get('patient_id')
    if helpers.delete_patient(patient_id):
        flash(f'患者档案 (ID: {patient_id}) 已被成功删除。', 'success')
    else:
        flash(f'删除患者档案 (ID: {patient_id}) 失败。', 'danger')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/services', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def admin_services():
    """为管理员提供管理诊所服务项目（价格表）的页面。"""
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_service':
            name = request.form.get('name')
            price = request.form.get('price')
            description = request.form.get('description')
            if name and price:
                try:
                    new_service = ServiceItem(name=name, price=float(price), description=description)
                    db.session.add(new_service)
                    db.session.commit()
                    flash('新的服务项目已成功添加。', 'success')
                except ValueError:
                    flash('价格必须是一个有效的数字。', 'danger')
                except Exception as e:
                    flash(f'添加服务时出错: {e}', 'danger')
                    db.session.rollback()
        
        elif action == 'delete_service':
            service_id = request.form.get('service_id')
            service_to_delete = ServiceItem.query.get(service_id)
            if service_to_delete:
                db.session.delete(service_to_delete)
                db.session.commit()
                flash('服务项目已成功删除。', 'success')

        return redirect(url_for('admin_services'))

    all_services = ServiceItem.query.order_by(ServiceItem.name).all()
    return render_template('admin_services.html', services=all_services)
# --- 医生功能路由 ---

# 营业额报表
@app.route('/doctor/financial_reports')
@login_required
@role_required('doctor')
def financial_reports():
    """为医生渲染一个独立的财务报表页面。"""
    # 页面只负责框架，所有数据由API异步加载
    return render_template('financial_reports.html')

#财务报表提供聚合数据的API接口
@app.route('/api/doctor/financial_reports_data')
@login_required
@role_required('doctor')
def api_financial_reports_data():
    """
    根据前端发来的请求参数（如时间范围、搜索关键词），
    查询并返回格式化后的财务数据。
    """
    doctor = helpers.get_user_by_username(session['user']['username'])
    
    # 从URL参数获取时间范围和搜索词
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    search_query = request.args.get('search', '')
    
    # 调用一个新的辅助函数来处理复杂的数据查询和计算
    data = helpers.get_financial_report_data(
        doctor_id=doctor.id, 
        start_date_str=start_date_str, 
        end_date_str=end_date_str, 
        search_query=search_query
    )
    
    return jsonify(data)

# 医生渲染数据报表页面
@app.route('/doctor/reports')
@login_required
@role_required('doctor')
def doctor_reports():
    """为医生渲染一个独立的数据报表和图表页面。"""
    # 这个路由只负责渲染页面框架，图表数据将由JavaScript异步加载。
    return render_template('doctor_reports.html')

# --- 账单功能 ---
@app.route('/doctor/patient/<int:patient_id>/invoice/new', methods=['GET', 'POST'])
@login_required
@role_required('doctor')
def create_invoice(patient_id):
    """为指定患者渲染创建新账单的页面，并处理表单提交。"""
    patient = Patient.query.get_or_404(patient_id)
    
    if request.method == 'POST':
        # ... POST 请求的处理逻辑保持不变 ...
        service_ids = request.form.getlist('service_ids[]')
        quantities = request.form.getlist('quantities[]')
        
        if not service_ids:
            flash('请至少选择一个服务项目。', 'danger')
            return redirect(url_for('create_invoice', patient_id=patient_id))
            
        total_amount = 0
        invoice_items_data = []

        for i, service_id in enumerate(service_ids):
            service = ServiceItem.query.get(service_id)
            quantity = int(quantities[i])
            if service and quantity > 0:
                subtotal = service.price * quantity
                total_amount += subtotal
                invoice_items_data.append({
                    'service_name': service.name,
                    'quantity': quantity,
                    'unit_price': service.price
                })
        
        try:
            new_invoice = Invoice(patient_id=patient.id, total_amount=total_amount)
            db.session.add(new_invoice)
            db.session.flush() 

            for item_data in invoice_items_data:
                new_item = InvoiceItem(invoice_id=new_invoice.id, **item_data)
                db.session.add(new_item)
            
            db.session.commit()
            flash('新账单已成功创建！', 'success')
            return redirect(url_for('patient_detail', patient_id=patient_id, tab='finance'))
        except Exception as e:
            db.session.rollback()
            flash(f'创建账单时发生错误: {e}', 'danger')

    # 1. 正常从数据库查询出所有 ServiceItem 对象
    services_objects = ServiceItem.query.order_by(ServiceItem.name).all()
    
    # 2. 手动将对象列表转换为字典列表
    services_data = [
        {'id': s.id, 'name': s.name, 'price': s.price}
        for s in services_objects
    ]
    
    # 3. 将这个可被JSON序列化的字典列表传递给模板
    return render_template('create_invoice.html', patient=patient, services=services_data)

@app.route('/doctor/invoice/<int:invoice_id>', methods=['GET', 'POST'])
@login_required
@role_required('doctor')
def invoice_detail(invoice_id):
    """显示单个账单的详情，并处理新付款的记录。"""
    invoice = Invoice.query.get_or_404(invoice_id)
    
    # 确保医生只能访问自己患者的账单
    if invoice.patient.doctor.username != session['user']['username']:
        flash('您没有权限访问此账单。', 'danger')
        return redirect(url_for('doctor_dashboard'))

    if request.method == 'POST':
        # 处理记录新付款的表单提交
        amount_str = request.form.get('amount')
        method = request.form.get('method')
        notes = request.form.get('notes')
        
        try:
            amount = float(amount_str)
            if amount <= 0:
                flash('付款金额必须为正数。', 'danger')
            else:
                # 调用一个辅助函数来处理付款逻辑
                success, message = helpers.record_payment_for_invoice(invoice.id, amount, method, notes)
                flash(message, 'success' if success else 'danger')
        except (ValueError, TypeError):
            flash('无效的金额格式。', 'danger')
        
        return redirect(url_for('invoice_detail', invoice_id=invoice.id))

    # GET请求：渲染账单详情页
    return render_template('invoice_detail.html', invoice=invoice)

@app.route('/doctor/dashboard')
@login_required
@role_required('doctor')

def doctor_dashboard():
    """渲染医生仪表盘，展示其名下的所有患者，并提供搜索功能。"""
    search_query = request.args.get('search', '')
    doctor = helpers.get_user_by_username(session['user']['username'])
    # 从医生的关联关系中查询患者
    my_patients_query = doctor.patients 
    if search_query:
        # 在该医生的患者中进行搜索
        my_patients_query = my_patients_query.filter(
            db.or_(Patient.name.ilike(f'%{search_query}%'), Patient.contact.ilike(f'%{search_query}%'))
        )
    return render_template('doctor_dashboard.html', patients=my_patients_query.all(), search_query=search_query)

@app.route('/doctor/patient/add', methods=['POST'])
@login_required
@role_required('doctor')
def add_new_patient():
    """处理医生添加新患者的请求。"""
    patient_data = request.form.to_dict()
    patient_data['doctor_username'] = session['user']['username']
    if not patient_data.get('name'):
        flash('患者姓名是必填项。', 'danger')
    else:
        patient, message = helpers.add_patient(patient_data)
        flash(message, 'success' if patient else 'danger')
    return redirect(url_for('doctor_dashboard'))

@app.route('/doctor/patient/<int:patient_id>', methods=['GET', 'POST'])
@login_required
@role_required('doctor')
def patient_detail(patient_id):
    """显示特定患者的详细信息页面，并处理患者信息的更新请求。"""
    patient = helpers.get_patient_by_id(patient_id)
    if not patient or patient.doctor.username != session['user']['username']:
        flash('找不到该患者或您没有权限访问。', 'danger')
        return redirect(url_for('doctor_dashboard'))
    if request.method == 'POST':
        if helpers.update_patient(patient_id, request.form.to_dict()):
            flash('患者信息更新成功！', 'success')
            return redirect(url_for('patient_detail', patient_id=patient_id, tab='record'))
        else:
            flash('更新失败，请重试。', 'danger')
    xrays = patient.xrays.order_by(desc(XRay.upload_date)).all()
    prescriptions = patient.prescriptions.order_by(desc(Prescription.date_issued)).all()
    return render_template(
        'patient_detail.html', 
        patient=patient, 
        xrays=xrays, 
        prescriptions=prescriptions, 
        Chinese_Name_Mapping=helpers.Chinese_Name_Mapping,
        Invoice=Invoice,  # 新增此行
        desc=desc         # 新增此行
    )

@app.route('/doctor/patient/<int:patient_id>/xray/upload', methods=['POST'])
@login_required
@role_required('doctor')
def upload_xray(patient_id):
    """处理医生从网页手动上传X光片的请求。"""
    if 'xray_file' not in request.files or not request.files['xray_file'].filename:
        flash('没有选择文件或文件无效。', 'warning')
        return redirect(url_for('patient_detail', patient_id=patient_id, tab='xrays'))
    file = request.files['xray_file']
    try:
        original_filename = secure_filename(file.filename)
        unique_filename = f"{patient_id}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{original_filename}"
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
        file.save(file_path)
        # 手动上传的图片同样会经过AI分析流程
        ai_results = helpers.run_yolo_inference(file_path)
        img_original = Image.open(file_path)
        img_with_overlays = helpers.draw_overlays_on_image(img_original, ai_results)
        base, _ = os.path.splitext(unique_filename)
        overlay_filename = f"overlay_{base}.png"
        overlay_path = os.path.join(app.config['UPLOAD_FOLDER'], overlay_filename)
        img_with_overlays.save(overlay_path)
        xray_data = {'filename': unique_filename, 'upload_date': datetime.datetime.now(), 'ai_results': ai_results}
        if helpers.add_xray_to_patient(patient_id, xray_data):
            flash('X光片上传并识别成功！', 'success')
        else:
            flash('X光片信息保存失败。', 'danger')
    except Exception as e:
        print(f"上传或处理X光片时发生严重错误: {e}")
        traceback.print_exc()
        flash('处理文件时发生严重错误，请联系管理员。', 'danger')
    return redirect(url_for('patient_detail', patient_id=patient_id, tab='xrays'))

@app.route('/doctor/xray/<int:xray_id>/delete', methods=['POST'])
@login_required
@role_required('doctor')
def delete_xray(xray_id):
    """处理医生删除X光片的请求。"""
    xray = XRay.query.get_or_404(xray_id)
    if xray.patient.doctor.username != session['user']['username']:
        flash('您没有权限删除此X光片。', 'danger')
        return redirect(request.referrer or url_for('doctor_dashboard'))
    patient_id = xray.patient_id
    if helpers.delete_xray_from_patient(xray_id):
        flash('X光片已成功删除。', 'success')
    else:
        flash('删除X光片失败。', 'danger')
    return redirect(url_for('patient_detail', patient_id=patient_id, tab='xrays'))
# 日历功能
@app.route('/doctor/appointments/calendar')
@login_required
@role_required('doctor')
def doctor_appointments_calendar():
    """渲染医生的预约日历视图。"""
    doctor = helpers.get_user_by_username(session['user']['username'])
    try:
        year = int(request.args.get('year', datetime.datetime.now().year))
        month = int(request.args.get('month', datetime.datetime.now().month))
    except ValueError:
        year, month = datetime.datetime.now().year, datetime.datetime.now().month
    _, num_days = calendar.monthrange(year, month)
    start_date = datetime.date(year, month, 1)
    end_date = datetime.date(year, month, num_days)
    appointments_this_month = Appointment.query.filter(Appointment.doctor_id == doctor.id, db.func.date(Appointment.appointment_time) >= start_date, db.func.date(Appointment.appointment_time) <= end_date).order_by(Appointment.appointment_time).all()
    appointments_by_day = defaultdict(list)
    for appt in appointments_this_month:
        appointments_by_day[appt.appointment_time.day].append(appt)
    cal = calendar.Calendar(firstweekday=6)
    month_calendar = cal.monthdatescalendar(year, month)
    prev_month_date = start_date - datetime.timedelta(days=1)
    next_month_date = end_date + datetime.timedelta(days=1)
    return render_template('doctor_appointments_calendar.html', year=year, month=month, month_calendar=month_calendar, appointments_by_day=appointments_by_day, today=datetime.date.today(), prev_month_url=url_for('doctor_appointments_calendar', year=prev_month_date.year, month=prev_month_date.month), next_month_url=url_for('doctor_appointments_calendar', year=next_month_date.year, month=next_month_date.month))

@app.route('/doctor/appointments/list')
@login_required
@role_required('doctor')
def doctor_appointments_list():
    """为医生渲染一个包含所有预约的表格视图，并提供搜索功能。"""
    doctor = helpers.get_user_by_username(session['user']['username'])
    search_query = request.args.get('search', '')
    
    # 从该医生的所有预约中进行查询
    query = doctor.appointments
    
    if search_query:
        # 搜索逻辑：可以根据患者姓名或预约事由进行搜索
        query = query.join(Patient).filter(
            db.or_(
                Patient.name.ilike(f'%{search_query}%'),
                Appointment.reason.ilike(f'%{search_query}%')
            )
        )
        
    # 按预约时间降序排列
    appointments = query.order_by(desc(Appointment.appointment_time)).all()
    
    return render_template('doctor_appointments_list.html', appointments=appointments, search_query=search_query)

@app.route('/doctor/appointment/<int:appointment_id>')
@login_required
@role_required('doctor')
def appointment_detail_doctor_view(appointment_id):
    """显示医生视角的预约详情。"""
    appointment = helpers.get_appointment_by_id(appointment_id)
    if not appointment or appointment.doctor.username != session['user']['username']:
        flash('找不到该预约或您没有权限访问。', 'danger')
        return redirect(url_for('doctor_appointments'))
    return render_template('appointment_detail_doctor_view.html', appointment=appointment)

@app.route('/doctor/appointment/update_status', methods=['POST'])
@login_required
@role_required('doctor')
def update_appointment_status():
    """处理医生更新预约状态的请求（例如，确诊、取消）。"""
    appointment_id = request.form.get('appointment_id')
    status = request.form.get('status')
    appt = helpers.get_appointment_by_id(appointment_id)
    if not appt or appt.doctor.username != session['user']['username']:
        flash('权限不足，操作失败。', 'danger')
        return redirect(url_for('doctor_appointments'))
    if helpers.update_appointment_status(appointment_id, status):
        flash(f'预约状态已成功更新为“{status}”', 'success')
    else:
        flash('更新预约状态失败，无效的状态值。', 'danger')
    return redirect(url_for('appointment_detail_doctor_view', appointment_id=appointment_id))

@app.route('/doctor/patient/<int:patient_id>/prescription/new', methods=['GET', 'POST'])
@login_required
@role_required('doctor')
def new_prescription(patient_id):
    """为患者渲染新的处方表单，并处理新处方的提交。"""
    patient = helpers.get_patient_by_id(patient_id)
    if not patient or patient.doctor.username != session['user']['username']:
        flash('找不到该患者或您没有权限访问。', 'danger')
        return redirect(url_for('doctor_dashboard'))
    if request.method == 'POST':
        prescription, message = helpers.add_prescription(form_data=request.form, patient_id=patient.id, doctor_username=session['user']['username'])
        if prescription:
            flash(message, 'success')
            return redirect(url_for('patient_detail', patient_id=prescription.patient_id, tab='prescriptions'))
        else:
            flash(message, 'danger')
    return render_template('prescription_form.html', patient=patient, prescription=None)

@app.route('/doctor/prescription/<int:prescription_id>/edit', methods=['GET', 'POST'])
@login_required
@role_required('doctor')
def edit_prescription(prescription_id):
    """渲染现有处方的编辑表单，并处理处方更新的提交。"""
    prescription = helpers.get_prescription_by_id(prescription_id)
    if not prescription or prescription.doctor.username != session['user']['username']:
        flash('找不到该处方或您没有权限访问。', 'danger')
        return redirect(url_for('doctor_dashboard'))
    if request.method == 'POST':
        updated, message = helpers.update_prescription(prescription_id, request.form)
        flash(message, 'success' if updated else 'danger')
        if updated:
            return redirect(url_for('patient_detail', patient_id=prescription.patient_id, tab='prescriptions'))
    return render_template('prescription_form.html', patient=prescription.patient, prescription=prescription)

@app.route('/doctor/prescription/<int:prescription_id>/delete', methods=['POST'])
@login_required
@role_required('doctor')
def delete_prescription(prescription_id):
    """处理医生删除处方的请求。"""
    prescription = helpers.get_prescription_by_id(prescription_id)
    if not prescription or prescription.doctor.username != session['user']['username']:
        flash('权限不足，操作失败。', 'danger')
        return redirect(request.referrer or url_for('doctor_dashboard'))
    patient_id = prescription.patient_id
    if helpers.delete_prescription(prescription_id):
        flash('处方已成功删除。', 'success')
    else:
        flash('删除处方失败。', 'danger')
    return redirect(url_for('patient_detail', patient_id=patient_id, tab='prescriptions'))

@app.route('/doctor/patient/<int:patient_id>/dental_chart')
@login_required
@role_required('doctor')
def dental_chart(patient_id):
    """渲染并计算牙位图的SVG坐标，以供前端显示。"""
    patient = helpers.get_patient_by_id(patient_id)
    if not patient or patient.doctor.username != session['user']['username']:
        flash('找不到该患者或您没有权限访问。', 'danger')
        return redirect(url_for('doctor_dashboard'))
    # 以下为动态生成牙齿图位置的逻辑
    teeth_layout_data = {'upper_arch': [], 'lower_arch': []}
    upper_arch_ids = [18, 17, 16, 15, 14, 13, 12, 11, 21, 22, 23, 24, 25, 26, 27, 28]
    lower_arch_ids = [48, 47, 46, 45, 44, 43, 42, 41, 31, 32, 33, 34, 35, 36, 37, 38]
    cx_upper, cy_upper = 400, 150
    rx_upper, ry_upper = 280, 200
    for i, tooth_id in enumerate(upper_arch_ids):
        angle = math.pi - (math.pi * (i + 0.5) / len(upper_arch_ids))
        x = cx_upper + rx_upper * math.cos(angle)
        y = cy_upper - ry_upper * math.sin(angle)
        rotation = 0
        teeth_layout_data['upper_arch'].append({'id': tooth_id, 'x': x, 'y': y, 'rotation': rotation})
    cx_lower, cy_lower = 400, 320
    rx_lower, ry_lower = 250, 180
    for i, tooth_id in enumerate(lower_arch_ids):
        angle = math.pi + (math.pi * (i + 0.5) / len(lower_arch_ids))
        x = cx_lower + rx_lower * math.cos(angle)
        y = cy_lower - ry_lower * math.sin(angle)
        rotation = 0
        teeth_layout_data['lower_arch'].append({'id': tooth_id, 'x': x, 'y': y, 'rotation': rotation})
    return render_template('dental_chart.html', patient=patient, dental_chart_data=patient.dental_chart, teeth_layout_data=teeth_layout_data)


@app.route('/doctor/patient/<int:patient_id>/dental_chart/save', methods=['POST'])
@login_required
@role_required('doctor')
def save_dental_chart(patient_id):
    """接收前端通过AJAX/Fetch提交的牙位图JSON数据并保存到数据库。"""
    patient = helpers.get_patient_by_id(patient_id)
    if not patient or patient.doctor.username != session['user']['username']:
        return jsonify({'status': 'error', 'message': '权限不足'}), 403
    
    chart_data = request.get_json()
    if chart_data is None:
        return jsonify({'status': 'error', 'message': '无效的数据'}), 400

    patient.dental_chart = chart_data
    db.session.commit()
    
    return jsonify({'status': 'success', 'message': '牙位图已保存！'})

@app.route('/api/doctor/dashboard_stats')
@login_required
@role_required('doctor')
def api_doctor_dashboard_stats():
    """为医生仪表盘和报表页面提供统计数据的API接口。"""
    doctor = helpers.get_user_by_username(session['user']['username'])
    # 【修改】调用新的数据聚合函数
    stats = helpers.get_doctor_report_data(doctor.id)
    return jsonify(stats)
# --- 患者功能路由 ---

@app.route('/patient/dashboard')
@login_required
@role_required('patient')
def patient_dashboard():
    """渲染患者个人仪表盘，展示其个人信息、X光片和处方。"""
    patient = helpers.get_patient_by_name(session['user']['username'])
    # 如果是新注册的患者，自动为其创建档案并关联到系统中的第一个医生。
    if not patient:
        first_doctor = User.query.filter_by(role='doctor').first()
        if first_doctor:
            patient, _ = helpers.add_patient({'name': session['user']['username'], 'doctor_username': first_doctor.username})
            flash(f'我们为您创建了新的个人档案，并关联到 {first_doctor.username} 医生。', 'info')
        else:
            flash('系统中暂无医生，无法为您创建档案，请联系管理员。', 'warning')
            return render_template('patient_dashboard.html', patient=None)
    xrays = patient.xrays.order_by(desc(XRay.upload_date)).all()
    prescriptions = patient.prescriptions.order_by(desc(Prescription.date_issued)).all()
    return render_template('patient_dashboard.html', patient=patient, xrays=xrays, prescriptions=prescriptions)

@app.route('/patient/appointments', methods=['GET', 'POST'])
@login_required
@role_required('patient')
def patient_appointments():
    """渲染患者的预约管理页面，并处理新的预约提交。"""
    patient = helpers.get_patient_by_name(session['user']['username'])
    if not patient:
        flash('找不到您的档案，请联系管理员。', 'warning')
        return redirect(url_for('patient_dashboard'))
    if request.method == 'POST':
        appointment_data = {'patient_id': patient.id, 'appointment_time': request.form.get('appointment_time'), 'reason': request.form.get('reason')}
        _, message = helpers.add_appointment(appointment_data)
        flash(message, 'success')
        return redirect(url_for('patient_appointments'))
    appointments = patient.appointments.order_by(desc(Appointment.appointment_time)).all()
    return render_template('patient_appointments.html', patient=patient, appointments=appointments)

# --- 通用功能路由 ---

@app.route('/export/my_record')
@login_required
@role_required('patient')
def export_my_record_csv():
    """一个便捷路由，用于患者导览自己的病历。"""
    patient = helpers.get_patient_by_name(session['user']['username'])
    if not patient:
        flash('找不到您的病历记录。', 'danger')
        return redirect(url_for('patient_dashboard'))
    return redirect(url_for('export_patient_csv', patient_id=patient.id))

@app.route('/ai_test', methods=['GET', 'POST'])
@login_required
def ai_test_page():
    """提供一个AI测试页面，允许用户上传本地图片进行AI分析。"""
    if request.method == 'POST':
        if 'file' not in request.files or not request.files['file'].filename:
            flash('没有选择文件', 'warning')
            return render_template('ai_test_page.html')
        file = request.files['file']
        temp_filename = "temp_test_" + secure_filename(file.filename)
        original_filepath = os.path.join(app.config['UPLOAD_FOLDER'], temp_filename)
        file.save(original_filepath)
        ai_results = helpers.run_yolo_inference(original_filepath)
        img_original = Image.open(original_filepath)
        img_with_overlays = helpers.draw_overlays_on_image(img_original, ai_results)
        base, _ = os.path.splitext(temp_filename)
        overlay_filename = 'overlay_' + base + '.png'
        overlay_filepath = os.path.join(app.config['UPLOAD_FOLDER'], overlay_filename)
        img_with_overlays.save(overlay_filepath)
        return render_template('ai_test_page.html', results=ai_results, original_image=temp_filename, overlay_image=overlay_filename, Chinese_Name_Mapping=helpers.Chinese_Name_Mapping)
    return render_template('ai_test_page.html')

@app.route('/chat')
@login_required
def chat():
    """渲染AI聊天机器人界面。"""
    username = session['user']['username']
    role = session['user']['role']
    history = helpers.load_chat_history(username, role)
    system_context = ""
    # 如果是患者，自动加载其病历作为AI的上下文背景信息。
    if role == 'patient':
        patient = helpers.get_patient_by_name(username)
        if patient:
            system_context = helpers.format_medical_record_for_ai(patient)
    return render_template('chat.html', history=history, system_context=system_context, user_role=role)

@app.route('/chat/stream', methods=['POST'])
@login_required
def chat_stream():
    """处理AI聊天的流式响应。"""
    data = request.json
    messages = data.get('messages', [])
    user_role = session['user']['role']
    # 如果是医生，并使用了'@'提及患者，则自动注入该患者的病历信息。
    if user_role == 'doctor':
        last_user_message = messages[-1]['content']
        match = re.search(r'(?:@|根据)\s*([^\s,，的]+)', last_user_message)
        if match:
            patient_name = match.group(1).strip()
            patient = helpers.get_patient_by_name(patient_name)
            if patient and patient.doctor.username == session['user']['username']:
                patient_context = helpers.format_medical_record_for_ai(patient)
                context_message = {"role": "system", "content": patient_context, "is_context": True}
                messages.insert(-1, context_message)
            else:
                def error_stream():
                    yield f"错误：在您的名下未找到名为 “{patient_name}” 的患者，请检查姓名是否正确。"
                return Response(error_stream(), mimetype='text/event-stream')
    return Response(helpers.get_deepseek_response_stream(messages), mimetype='text/event-stream')

@app.route('/chat/save', methods=['POST'])
@login_required
def save_chat():
    """保存聊天记录。"""
    helpers.save_chat_history(session['user']['username'], session['user']['role'], request.json.get('history', []))
    return jsonify({"status": "success"})

@app.route('/prescription/<int:prescription_id>/print')
@login_required
def print_prescription(prescription_id):
    """提供一个用于打印处方的专用页面。"""
    prescription = helpers.get_prescription_by_id(prescription_id)
    if not prescription:
        flash('找不到该处方。', 'danger')
        return redirect(url_for('index'))
    current_user = helpers.get_user_by_username(session['user']['username'])
    is_admin = current_user.role == 'admin'
    is_doctor = prescription.doctor_id == current_user.id
    is_patient = (current_user.role == 'patient' and prescription.patient.name == current_user.username)
    if not (is_admin or is_doctor or is_patient):
        flash('您没有权限查看此内容。', 'danger')
        return redirect(url_for('index'))
    return render_template('prescription_print.html', prescription=prescription)

@app.route('/export/patient/<int:patient_id>')
@login_required
def export_patient_csv(patient_id):
    """生成并提供特定患者完整病历的CSV文件下载。"""
    patient = helpers.get_patient_by_id(patient_id)
    if not patient:
        flash('找不到该患者。', 'danger')
        return redirect(url_for('index'))
    current_user = helpers.get_user_by_username(session['user']['username'])
    is_admin = current_user.role == 'admin'
    is_doctor = patient.doctor_id == current_user.id
    is_patient = (current_user.role == 'patient' and patient.name == current_user.username)
    if not (is_admin or is_doctor or is_patient):
        flash('您没有权限导出该患者的病历。', 'danger')
        return redirect(url_for('index'))
    csv_data = helpers.generate_patient_record_csv(patient)
    if csv_data:
        response = make_response(csv_data)
        filename = f"{patient.name}_病历_{datetime.date.today().strftime('%Y%m%d')}.csv"
        # 设置HTTP头，使浏览器触发文件下载，并正确处理中文文件名。
        response.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(filename)}"
        response.headers['Content-Type'] = 'text/csv; charset=utf-8-sig'
        return response
    flash('生成病历CSV文件失败。', 'danger')
    return redirect(request.referrer or url_for('index'))

@app.route('/uploads/<filename>')
def serve_uploaded_file(filename):
    """提供一个URL端点，让浏览器可以安全地访问并显示位于`uploads`文件夹中的图片。"""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

#                     应用程序启动入口
def open_browser():
    """一个简单的函数，用于在默认浏览器中打开应用的首页。"""
    webbrowser.open_new("http://127.0.0.1:5000")

if __name__ == '__main__':
    from gevent import pywsgi
    from geventwebsocket.handler import WebSocketHandler

    print("服务器启动，WebSocket已启用 (引擎: gevent)。")
    
    # 创建一个1秒后执行的定时器，调用 open_browser 函数
    Timer(1, open_browser).start()

    # 创建并永久运行服务器
    server = pywsgi.WSGIServer(
        ('0.0.0.0', 5000),
        app,
        handler_class=WebSocketHandler
    )
    server.serve_forever()