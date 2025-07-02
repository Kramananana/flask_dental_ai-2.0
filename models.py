from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
import json
import datetime

db = SQLAlchemy()
bcrypt = Bcrypt()

class User(db.Model):
    """用户模型：存储管理员、医生和患者的账户信息"""
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    role = db.Column(db.String(20), nullable=False, index=True) # 为角色添加索引以优化查询

    # --- 关系定义 ---
    # 作为医生，关联其下的患者
    patients = db.relationship('Patient', backref='doctor', lazy='dynamic', foreign_keys='Patient.doctor_id', cascade="all, delete-orphan")
    # 作为医生，关联其下的预约
    appointments = db.relationship('Appointment', backref='doctor', lazy='dynamic', foreign_keys='Appointment.doctor_id', cascade="all, delete-orphan")
    # 作为医生，关联其开具的处方
    prescriptions = db.relationship('Prescription', backref='doctor', lazy='dynamic', foreign_keys='Prescription.doctor_id', cascade="all, delete-orphan")
    
    def set_password(self, password):
        """设置密码，使用bcrypt进行哈希加密"""
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        """校验密码"""
        return bcrypt.check_password_hash(self.password_hash, password)

class Patient(db.Model):
    """患者模型：存储患者的个人信息、病历、聊天记录和牙位图"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True, index=True)
    gender = db.Column(db.String(10))
    dob = db.Column(db.String(20)) # 出生日期
    contact = db.Column(db.String(50))
    
    # --- 病历核心字段 ---
    chief_complaint = db.Column(db.Text) # 主诉
    present_illness = db.Column(db.Text) # 现病史
    past_history = db.Column(db.Text) # 既往史
    examination_info = db.Column(db.Text) # 检查信息
    differential_diagnosis = db.Column(db.Text) # 鉴别诊断
    treatment_plan = db.Column(db.Text) # 治疗计划
    
    # 创建日期字段
    creation_date = db.Column(db.DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    # --- JSON格式存储的动态数据 ---
    chat_history_json = db.Column(db.Text, name='chat_history') # 与AI的聊天记录
    dental_chart_json = db.Column(db.Text, name='dental_chart') # 牙位图标记数据

    # 使用property将JSON字符串转换为Python对象，方便调用
    @property
    def chat_history(self):
        return json.loads(self.chat_history_json) if self.chat_history_json else []

    @chat_history.setter
    def chat_history(self, history):
        self.chat_history_json = json.dumps(history, ensure_ascii=False)

    @property
    def dental_chart(self):
        return json.loads(self.dental_chart_json) if self.dental_chart_json else {}

    @dental_chart.setter
    def dental_chart(self, chart_data):
        self.dental_chart_json = json.dumps(chart_data, ensure_ascii=False)

    # --- 关系与外键 ---
    doctor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    xrays = db.relationship('XRay', backref='patient', lazy='dynamic', cascade="all, delete-orphan")
    appointments = db.relationship('Appointment', backref='patient', lazy='dynamic', cascade="all, delete-orphan")
    prescriptions = db.relationship('Prescription', backref='patient', lazy='dynamic', cascade="all, delete-orphan")
    invoices = db.relationship('Invoice', backref='patient', lazy='dynamic', cascade="all, delete-orphan")

# 在 models.py 中
class XRay(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(200), nullable=False)
    # 这里没有 overlay_filename 字段
    upload_date = db.Column(db.DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    ai_results_json = db.Column(db.Text, name='ai_results')
    
    @property
    def ai_results(self):
        return json.loads(self.ai_results_json) if self.ai_results_json else []

    @ai_results.setter
    def ai_results(self, results):
        self.ai_results_json = json.dumps(results)

    patient_id = db.Column(db.Integer, db.ForeignKey('patient.id'), nullable=False)

class Appointment(db.Model):
    """预约模型"""
    id = db.Column(db.Integer, primary_key=True)
    appointment_time = db.Column(db.DateTime, nullable=False)
    reason = db.Column(db.Text, nullable=True) # 预约事由
    status = db.Column(db.String(20), nullable=False, default='待确认') # 状态: 待确认, 已确认, 已完成, 已取消
    
    patient_id = db.Column(db.Integer, db.ForeignKey('patient.id'), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

class Prescription(db.Model):
    """电子处方模型"""
    id = db.Column(db.Integer, primary_key=True)
    date_issued = db.Column(db.Date, nullable=False, default=datetime.date.today) # 开具日期
    medications_json = db.Column(db.Text, name='medications', nullable=False) # 药品列表
    
    patient_id = db.Column(db.Integer, db.ForeignKey('patient.id'), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    @property
    def medications(self):
        """将JSON字符串的药品列表转换为Python的list"""
        return json.loads(self.medications_json) if self.medications_json else []

    @medications.setter
    def medications(self, meds_list):
        self.medications_json = json.dumps(meds_list, ensure_ascii=False)
# --- 计费功能 ---
class ServiceItem(db.Model):
    """服务项目模型：存储诊所提供的所有可计费服务及其标准价格"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=True)
    price = db.Column(db.Float, nullable=False)

class Invoice(db.Model):
    """账单模型：代表一张开具给患者的账单"""
    id = db.Column(db.Integer, primary_key=True)
    issue_date = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    due_date = db.Column(db.DateTime, nullable=True)
    total_amount = db.Column(db.Float, nullable=False)
    paid_amount = db.Column(db.Float, nullable=False, default=0.0)
    status = db.Column(db.String(30), nullable=False, default='未支付') # 例如: '未支付', '部分支付', '已付清', '已逾期'
    
    patient_id = db.Column(db.Integer, db.ForeignKey('patient.id'), nullable=False)
    
    # 关系：一张账单包含多个账单条目
    items = db.relationship('InvoiceItem', backref='invoice', lazy=True, cascade="all, delete-orphan")
    # 关系：一张账单可以有多条付款记录
    payments = db.relationship('Payment', backref='invoice', lazy=True, cascade="all, delete-orphan")

class InvoiceItem(db.Model):
    """账单条目模型：代表账单中的每一行服务项目"""
    id = db.Column(db.Integer, primary_key=True)
    service_name = db.Column(db.String(150), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Float, nullable=False) # 记录开单时的价格，以防服务标准价格变动
    
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoice.id'), nullable=False)

class Payment(db.Model):
    """付款记录模型：记录针对某张账单的每一次付款"""
    id = db.Column(db.Integer, primary_key=True)
    payment_date = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    amount = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(50), nullable=True) # 例如: '现金', '刷卡', '微信支付', '支付宝'
    notes = db.Column(db.Text, nullable=True)
    
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoice.id'), nullable=False)