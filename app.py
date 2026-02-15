# app.py
import os
import json
from datetime import datetime, timedelta
from io import BytesIO
from threading import Thread

from flask import (Flask, render_template, request, redirect, url_for, flash,
                   send_from_directory)
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                       login_required, current_user)
from flask_bcrypt import Bcrypt
from werkzeug.utils import secure_filename

# Bibliotecas para manipulação de PDF
from pypdf import PdfWriter
import fitz  # PyMuPDF
from weasyprint import HTML

# --- 1. INICIALIZAÇÃO E CONFIGURAÇÃO DA APLICAÇÃO ---
app = Flask(__name__)

# Configurações essenciais
app.config['SECRET_KEY'] = 'uma-chave-secreta-muito-segura-e-dificil-de-adivinhar'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///site.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['REPORTS_FOLDER'] = 'reports'

# Garante que as pastas de upload e relatórios existem
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['REPORTS_FOLDER'], exist_ok=True)

# --- CORREÇÃO: Adicionados novos tipos de processo ---
WORKFLOWS = {
    "PAD": ["Autuado", "Instrução", "Relatório Final", "Julgamento", "Finalizado"],
    "Sindicância": ["Autuado", "Apuração", "Relatório Final", "Arquivado"],
    "Tomada de Conta Especial": ["Instauração", "Citação", "Defesa", "Relatório", "Julgamento", "Finalizado"],
    "Processo Administrativo Especial": ["Autuação", "Instrução", "Decisão", "Finalizado"]
}

# --- 2. INICIALIZAÇÃO DAS EXTENSÕES ---
db = SQLAlchemy(app)
migrate = Migrate(app, db)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = "Por favor, faça login para aceder a esta página."
login_manager.login_message_category = 'info'

# --- 3. DEFINIÇÃO DOS MODELOS DA BASE DE DADOS ---
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(60), nullable=False)

class Processo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    numero_processo = db.Column(db.String(50), unique=True, nullable=False)
    portaria = db.Column(db.String(100), nullable=True)
    membros_comissao = db.Column(db.Text, nullable=True) # Armazenado como JSON string
    tipo = db.Column(db.String(50), nullable=False)
    servidor_a_apurar = db.Column(db.Boolean, nullable=False, server_default='0')
    servidor_investigado = db.Column(db.String(200), nullable=True)
    servidor_cargo = db.Column(db.String(100), nullable=True)
    servidor_matricula = db.Column(db.String(50), nullable=True)
    resumo_fatos = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(50), nullable=False, default='Autuado')
    data_autuacao = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    prazo_inicial_dias = db.Column(db.Integer, nullable=True)
    prorrogacao_dias = db.Column(db.Integer, nullable=True, default=0)
    data_atualizacao = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    andamentos = db.relationship('Andamento', backref='processo', lazy=True, cascade="all, delete-orphan")

class Andamento(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    etapa = db.Column(db.String(100), nullable=False)
    descricao = db.Column(db.Text, nullable=True)
    data = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    processo_id = db.Column(db.Integer, db.ForeignKey('processo.id'), nullable=False)
    documentos = db.relationship('Documento', backref='andamento', lazy=True, cascade="all, delete-orphan")

class Documento(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    andamento_id = db.Column(db.Integer, db.ForeignKey('andamento.id'), nullable=False)

class Agenda(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tarefa = db.Column(db.Text, nullable=False)
    prazo = db.Column(db.Date, nullable=True)
    concluida = db.Column(db.Boolean, default=False, nullable=False)

# --- Funções Auxiliares e de Contexto ---
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.context_processor
def inject_global_vars():
    """ Injeta variáveis globais nos templates Jinja2. """
    return {
        'now': datetime.utcnow(),
        'timedelta': timedelta,
        'WORKFLOWS': WORKFLOWS,
        'json': json
    }

def _processar_membros_comissao(form):
    """ Processa e formata os membros da comissão para armazenamento. """
    membros_nomes = form.getlist('membro_nome')
    membros_funcoes = form.getlist('membro_funcao')
    comissao_lista = [{'nome': nome, 'funcao': funcao} for nome, funcao in zip(membros_nomes, membros_funcoes) if nome]
    return json.dumps(comissao_lista, ensure_ascii=False)

# --- 4. Rotas da Aplicação ---

# Rotas de Autenticação
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and bcrypt.check_password_hash(user.password, request.form['password']):
            login_user(user, remember=True)
            return redirect(url_for('index'))
        else:
            flash('Login sem sucesso. Verifique o nome de utilizador e a senha.', 'danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    # Permitir registo apenas se não houver utilizadores ou se o admin estiver logado
    if User.query.count() > 0 and not current_user.is_authenticated:
        flash('O registo de novos utilizadores não está permitido.', 'warning')
        return redirect(url_for('login'))
    if request.method == 'POST':
        hashed_password = bcrypt.generate_password_hash(request.form['password']).decode('utf-8')
        user = User(username=request.form['username'], password=hashed_password)
        db.session.add(user)
        db.session.commit()
        flash('A sua conta foi criada! Já pode fazer login.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

# Rota Principal (Dashboard)
@app.route('/')
@login_required
def index():
    processos = Processo.query.order_by(Processo.data_autuacao.desc()).all()
    tarefas_agenda = Agenda.query.order_by(Agenda.concluida, Agenda.prazo.asc()).all()

    total_processos = len(processos)
    finalizados = sum(1 for p in processos if p.status in ['Finalizado', 'Arquivado'])
    em_andamento = total_processos - finalizados
    
    hoje = datetime.utcnow().date()
    limite_vencimento = hoje + timedelta(days=7)
    prazos_vencendo = 0
    for p in processos:
        if p.status not in ['Finalizado', 'Arquivado'] and p.prazo_inicial_dias:
            prazo_total = p.prazo_inicial_dias + (p.prorrogacao_dias or 0)
            data_final = p.data_autuacao.date() + timedelta(days=prazo_total)
            if hoje <= data_final < limite_vencimento:
                prazos_vencendo += 1
    
    totais = {
        "total": total_processos, "em_andamento": em_andamento,
        "finalizados": finalizados, "prazos_vencendo": prazos_vencendo
    }
    return render_template('index.html', processos=processos, tarefas=tarefas_agenda, totais=totais)

# Rotas de Gestão de Processos (CRUD)
@app.route('/processo/adicionar', methods=['GET', 'POST'])
@login_required
def adicionar_processo():
    if request.method == 'POST':
        numero_processo_form = request.form['numero_processo']
        
        # --- VERIFICAÇÃO ADICIONADA AQUI ---
        # Verifica se já existe um processo com este número
        processo_existente = Processo.query.filter_by(numero_processo=numero_processo_form).first()
        if processo_existente:
            flash(f'Erro: O número de processo "{numero_processo_form}" já existe.', 'danger')
            return redirect(url_for('adicionar_processo'))
        
        data_autuacao_obj = datetime.strptime(request.form['data_autuacao'], '%Y-%m-%d')
        tipo_processo = request.form['tipo']
        status_inicial = WORKFLOWS.get(tipo_processo, ["Autuado"])[0]
        
        novo_processo = Processo(
            numero_processo=numero_processo_form,
            tipo=tipo_processo,
            resumo_fatos=request.form['resumo_fatos'],
            data_autuacao=data_autuacao_obj,
            portaria=request.form.get('portaria'),
            membros_comissao=_processar_membros_comissao(request.form),
            status=status_inicial,
            servidor_a_apurar='servidor_a_apurar' in request.form,
            servidor_investigado=request.form.get('servidor_investigado'),
            servidor_cargo=request.form.get('servidor_cargo'),
            servidor_matricula=request.form.get('servidor_matricula'),
            prazo_inicial_dias=request.form.get('prazo_inicial_dias', type=int),
            prorrogacao_dias=request.form.get('prorrogacao_dias', 0, type=int)
        )
        primeiro_andamento = Andamento(etapa=status_inicial, processo=novo_processo, descricao="Processo instaurado.")
        db.session.add(novo_processo)
        db.session.add(primeiro_andamento)
        db.session.commit()
        flash('Processo adicionado com sucesso!', 'success')
        return redirect(url_for('index'))
    return render_template('adicionar_processo.html')

@app.route('/processo/<int:processo_id>')
@login_required
def detalhes_processo(processo_id):
    processo = Processo.query.get_or_404(processo_id)
    andamentos_ordenados = sorted(processo.andamentos, key=lambda x: x.data, reverse=True)
    return render_template('detalhes_processo.html', processo=processo, andamentos_ordenados=andamentos_ordenados)

@app.route('/processo/<int:processo_id>/editar', methods=['GET', 'POST'])
@login_required
def editar_processo(processo_id):
    processo = Processo.query.get_or_404(processo_id)
    if request.method == 'POST':
        processo.data_autuacao = datetime.strptime(request.form['data_autuacao'], '%Y-%m-%d')
        processo.membros_comissao = _processar_membros_comissao(request.form)
        processo.numero_processo = request.form['numero_processo']
        processo.tipo = request.form['tipo']
        processo.resumo_fatos = request.form['resumo_fatos']
        processo.portaria = request.form.get('portaria')
        processo.servidor_a_apurar = 'servidor_a_apurar' in request.form
        processo.servidor_investigado = request.form.get('servidor_investigado')
        processo.servidor_cargo = request.form.get('servidor_cargo')
        processo.servidor_matricula = request.form.get('servidor_matricula')
        processo.prazo_inicial_dias = request.form.get('prazo_inicial_dias', type=int)
        processo.prorrogacao_dias = request.form.get('prorrogacao_dias', 0, type=int)
        db.session.commit()
        flash('Processo atualizado com sucesso!', 'success')
        return redirect(url_for('detalhes_processo', processo_id=processo.id))
    return render_template('editar_processo.html', processo=processo)

@app.route('/processo/<int:processo_id>/excluir', methods=['POST'])
@login_required
def excluir_processo(processo_id):
    processo = Processo.query.get_or_404(processo_id)
    db.session.delete(processo)
    db.session.commit()
    flash('Processo excluído com sucesso.', 'success')
    return redirect(url_for('index'))

# Rotas de Andamentos
@app.route('/processo/<int:processo_id>/avancar_etapa', methods=['POST'])
@login_required
def avancar_etapa(processo_id):
    processo = Processo.query.get_or_404(processo_id)
    nova_etapa = request.form.get('nova_etapa')
    descricao = request.form.get('descricao')

    if not nova_etapa:
        flash('É necessário selecionar uma nova etapa.', 'danger')
        return redirect(url_for('detalhes_processo', processo_id=processo_id))

    processo.status = nova_etapa
    novo_andamento = Andamento(
        etapa=nova_etapa,
        descricao=descricao,
        processo_id=processo_id
    )

    # Lidar com upload de múltiplos ficheiros
    files = request.files.getlist('documentos')
    for file in files:
        if file and file.filename != '':
            if not file.filename.lower().endswith('.pdf'):
                flash(f'Ficheiro "{file.filename}" ignorado. Apenas PDFs são permitidos.', 'warning')
                continue
            filename = secure_filename(f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{file.filename}")
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            documento = Documento(filename=filename, andamento=novo_andamento)
            db.session.add(documento)

    db.session.add(novo_andamento)
    db.session.commit()
    flash(f'Processo avançado para a etapa: {nova_etapa}.', 'success')
    return redirect(url_for('detalhes_processo', processo_id=processo_id))


# Rotas da Agenda
@app.route('/agenda/adicionar', methods=['POST'])
@login_required
def adicionar_tarefa_agenda():
    tarefa_texto = request.form.get('tarefa')
    prazo_str = request.form.get('prazo')
    if tarefa_texto:
        prazo_obj = datetime.strptime(prazo_str, '%Y-%m-%d').date() if prazo_str else None
        nova_tarefa = Agenda(tarefa=tarefa_texto, prazo=prazo_obj)
        db.session.add(nova_tarefa)
        db.session.commit()
        flash('Tarefa adicionada à agenda.', 'success')
    return redirect(url_for('index'))

@app.route('/agenda/concluir/<int:tarefa_id>')
@login_required
def concluir_tarefa(tarefa_id):
    tarefa = Agenda.query.get_or_404(tarefa_id)
    tarefa.concluida = not tarefa.concluida
    db.session.commit()
    return redirect(url_for('index'))

@app.route('/agenda/excluir/<int:tarefa_id>')
@login_required
def excluir_tarefa(tarefa_id):
    tarefa = Agenda.query.get_or_404(tarefa_id)
    db.session.delete(tarefa)
    db.session.commit()
    flash('Tarefa removida da agenda.', 'warning')
    return redirect(url_for('index'))

# Rotas de Geração e Download de PDFs
def generate_pdf_task(app_context, processo_id):
    """ Função executada em background para gerar o PDF consolidado. """
    with app_context:
        try:
            processo = Processo.query.get(processo_id)
            if not processo: return

            merger = PdfWriter()
            
            # 1. Gerar a capa
            capa_html = render_template('capa_processo.html', processo=processo)
            capa_pdf_bytes = HTML(string=capa_html).write_pdf()
            merger.append(BytesIO(capa_pdf_bytes))

            # 2. Adicionar documentos dos andamentos
            andamentos = sorted(processo.andamentos, key=lambda x: x.data)
            for andamento in andamentos:
                for doc in andamento.documentos:
                    caminho_doc = os.path.join(app.config['UPLOAD_FOLDER'], doc.filename)
                    if os.path.exists(caminho_doc):
                        merger.append(caminho_doc)
            
            # 3. Salvar PDF unido temporariamente
            merged_pdf_stream = BytesIO()
            merger.write(merged_pdf_stream)
            merger.close()
            merged_pdf_stream.seek(0)
            
            # 4. Adicionar número de página com PyMuPDF
            pdf_document = fitz.open(stream=merged_pdf_stream, filetype="pdf")
            for page_num, page in enumerate(pdf_document):
                page.insert_textbox(
                    page.rect, f"Fl. {page_num + 1:03d}",
                    fontsize=10, fontname="helv", color=(0, 0, 0),
                    align=fitz.TEXT_ALIGN_RIGHT,
                )
            
            # 5. Salvar o ficheiro final
            timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')
            final_filename = f"processo_{processo.numero_processo.replace('/', '-')}_{timestamp_str}.pdf"
            final_filepath = os.path.join(app.config['REPORTS_FOLDER'], final_filename)
            pdf_document.save(final_filepath)
            pdf_document.close()
            print(f"Relatório gerado: {final_filename}")

        except Exception as e:
            print(f"Erro ao gerar PDF para o processo {processo_id}: {e}")


@app.route('/processo/<int:processo_id>/exportar_documentos')
@login_required
def exportar_documentos(processo_id):
    thread = Thread(target=generate_pdf_task, args=(app.app_context(), processo_id))
    thread.start()
    flash('O seu relatório está a ser gerado em segundo plano. Verifique a página de "Relatórios" em breve.', 'info')
    return redirect(url_for('detalhes_processo', processo_id=processo_id))

@app.route('/relatorios')
@login_required
def relatorios_gerados():
    files = [f for f in os.listdir(app.config['REPORTS_FOLDER']) if f.endswith('.pdf')]
    files.sort(key=lambda x: os.path.getmtime(os.path.join(app.config['REPORTS_FOLDER'], x)), reverse=True)
    return render_template('relatorios.html', files=files)

@app.route('/relatorios/<path:filename>')
@login_required
def download_relatorio(filename):
    return send_from_directory(app.config['REPORTS_FOLDER'], filename, as_attachment=True)

@app.route('/uploads/<path:filename>')
@login_required
def download_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True)


# --- 5. PONTO DE ENTRADA DA APLICAÇÃO ---
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

