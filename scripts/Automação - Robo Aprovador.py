from playwright.async_api import async_playwright
from datetime import datetime
import os
import logging
import traceback
import asyncio

# ======================================
# CONFIGURAÇÕES
# ======================================.
URL = "http: www.seusite.com.br"
USUARIO = "usuário@emailempresa.com.br"
SENHA = "**************"
JUSTIFICATIVA = "Sua justificativa padrão aqui, que será preenchida automaticamente pelo robô."

# Se True, abre o navegador em uma janela visível (útil para acompanhar
# e depurar visualmente); se False, roda "invisível" (headless).
# Observação: essa variável está definida aqui, mas o valor efetivamente
# usado no Playwright é o headless=True fixado dentro de executar().
HEADLESS = False

# Timeouts centralizados (em milissegundos) - aumentados para dar folga a
# redirecionamentos e animações da interface, evitando falhas por lentidão
# pontual do sistema ou da rede.
TIMEOUT_PADRAO = 30000   # usado na maioria das buscas de elementos na tela
TIMEOUT_LOGIN = 45000    # usado especificamente nas etapas de login (mais lentas)

# ===================================================
# LOG
# ===================================================
# Configura o registro de eventos (logging) da execução em um arquivo
# chamado "aprovacao.log". Cada linha registrada no log traz data/hora,
# nível (INFO, ERROR etc.) e a mensagem, o que ajuda a auditar depois
# o que aconteceu em cada execução do robô, mesmo sem estar acompanhando
# a tela no momento.

logging.basicConfig(
    level=logging.INFO,
    filename="aprovacao.log",
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ===================================================

def dentro_do_horario():
    """
    Verifica se o momento atual está dentro do horário permitido para
    o robô funcionar: dias de semana (segunda a sexta, onde 0=segunda
    e 4=sexta) e entre 8h e 18h.

    Retorna True se estiver dentro da janela permitida, e False caso
    contrário. Essa função é útil como uma "trava de segurança" para
    quem for agendar a execução do robô (ex.: em uma tarefa agendada
    do Windows), evitando que ele rode fora do horário comercial.
    Observação: no fluxo atual do script (executar()), essa checagem
    não está sendo chamada antes de iniciar a automação.
    """

    agora = datetime.now()

    return (
        agora.weekday() <= 4 and
        8 <= agora.hour <= 18
    )

# ===================================================
# HELPER: localizar um elemento na página principal OU em qualquer frame
# Isso resolve o caso mais comum de timeout: o elemento existir só dentro
# de um <iframe>, e o código estar procurando apenas na página "de fora".
# ===================================================

async def localizar_em_frames(page, selector, timeout=TIMEOUT_PADRAO, descricao=""):
    """
    Procura um elemento (via seletor CSS) primeiro na página principal e,
    se não encontrar, varre todos os <iframe>s (frames) da página até
    achar o elemento visível ou até o tempo limite (timeout) se esgotar.

    Por que essa função existe: muitos sistemas web (inclusive este)
    carregam partes da tela dentro de iframes. Se o robô procurar um
    elemento só na página "de fora", ele nunca vai encontrar algo que
    está dentro do iframe — e vai parecer que o elemento "não existe",
    quando na verdade só está em outro contexto. Esta função tenta nos
    dois lugares para evitar esse erro comum.

    Parâmetros:
        page (Page): a página (ou aba) do Playwright onde procurar.
        selector (str): o seletor CSS do elemento (ex.: "#UserName").
        timeout (int): tempo máximo de busca nos frames, em milissegundos.
        descricao (str): texto amigável usado só nas mensagens de log/print,
            para deixar mais claro o que está sendo procurado.

    Retorna:
        Locator do Playwright apontando para o elemento encontrado.

    Lança uma Exception se o elemento não for encontrado (ou nunca ficar
    visível) dentro do tempo limite, nem na página nem em nenhum frame.
    """

    print(f"Procurando elemento {descricao or selector}...")

    # 1. tenta direto na página principal (timeout curto, é só uma checagem rápida)
    try:
        locator = page.locator(selector)
        await locator.wait_for(state="visible", timeout=3000)
        print(f"Elemento encontrado na página principal: {descricao or selector}")
        return locator
    except Exception:
        pass

    print("Não encontrado na página principal, verificando frames...")

    # 2. tenta em cada frame, re-obtendo page.frames a cada volta (frames podem
    # ser destruídos/recriados) e logando o motivo real de cada falha em vez
    # de engolir o erro silenciosamente.
    fim = asyncio.get_event_loop().time() + (timeout / 1000)
    tentativa = 0

    while asyncio.get_event_loop().time() < fim:

        tentativa += 1
        frames_atuais = list(page.frames)

        for frame in frames_atuais:

            if frame.is_detached():
                continue

            try:
                locator = frame.locator(selector)
                qtd = await locator.count()

                if qtd > 0:
                    # confirma visibilidade sem re-navegar o frame
                    try:
                        visivel = await locator.first.is_visible()
                    except Exception:
                        visivel = False

                    if visivel:
                        print(f"Elemento encontrado e visível no frame: {frame.url}")
                        return locator.first

                    # existe mas ainda não está visível: continua tentando
                    if tentativa % 6 == 0:
                        print(f"Elemento existe no frame {frame.url} mas ainda não está visível...")

            except Exception as erro_frame:
                if tentativa % 6 == 0:
                    print(f"Aviso: erro ao checar frame {frame.url}: {erro_frame}")
                continue

        await page.wait_for_timeout(500)

    raise Exception(
        f"Elemento '{descricao or selector}' não encontrado (ou nunca ficou visível) "
        f"nem na página nem em nenhum frame, após {timeout}ms."
    )

# ===================================================

async def login(page):
    """
    Realiza o login no sistema, do início ao fim:
      1. Abre a URL de login configurada.
      2. Aceita o banner de cookies, se ele aparecer.
      3. Preenche o campo de usuário e o campo de senha.
      4. Clica no botão de login.
      5. Registra um "diagnóstico" (URL, título e texto da página, além
         de um screenshot) logo após o clique — isso ajuda a entender,
         em caso de falha, se o login foi aceito ou travou em alguma
         tela intermediária (erro, verificação, etc.).
      6. Espera o ícone do menu principal aparecer, que é a confirmação
         de que o login deu certo e a aplicação carregou por completo.

    Parâmetros:
        page (Page): a página do Playwright onde o login será feito.

    Não retorna valor; ao final da execução, a página estará logada e
    pronta para navegação dentro do sistema.
    """

    print("Abrindo login...")

    await page.goto(URL, timeout=TIMEOUT_LOGIN)

    # Trata o banner de cookies, se aparecer (comum em perfil novo/limpo).
    # Se não aparecer (ex: perfil já tem a preferência salva), simplesmente
    # ignora e segue — por isso o timeout curto e o try/except.
    print("Verificando banner de cookies...")
    try:
        botao_cookies = page.get_by_text("Aceitar", exact=False)
        await botao_cookies.first.wait_for(state="visible", timeout=5000)
        await botao_cookies.first.click(force=True)
        print("Banner de cookies aceito.")
        await page.wait_for_timeout(500)
    except Exception:
        print("Banner de cookies não apareceu (ou já estava aceito). Seguindo normalmente.")

    campo_usuario = await localizar_em_frames(page, "#UserName", timeout=TIMEOUT_LOGIN, descricao="campo usuário")
    await campo_usuario.fill(USUARIO)

    campo_senha = await localizar_em_frames(page, "#Password", timeout=TIMEOUT_LOGIN, descricao="campo senha")
    await campo_senha.fill(SENHA)

    botao_login = await localizar_em_frames(page, "#btn-login", timeout=TIMEOUT_LOGIN, descricao="botão login")
    await botao_login.click(force=True)

    # DIAGNÓSTICO: registra o que carregou logo após o clique de login,
    # antes de procurar o ícone do menu. Isso mostra se o login realmente
    # foi aceito (redirecionou pra dentro do sistema) ou se ficou preso
    # em alguma tela intermediária (erro, verificação, CAPTCHA etc).
    await page.wait_for_timeout(3000)

    print("--- DIAGNÓSTICO: estado da página logo após clicar em login ---")
    print(f"URL atual: {page.url}")
    print(f"Título da página: {await page.title()}")

    try:
        await page.screenshot(path="debug_pos_login.png", full_page=True)
        print("Screenshot salvo em: debug_pos_login.png")
    except Exception as erro_shot:
        print(f"Não foi possível salvar screenshot pós-login: {erro_shot}")

    # Também salva o texto visível da página, útil pra pegar mensagens de
    # erro (ex: "usuário ou senha inválidos", "verificação necessária" etc.)
    # mesmo sem conseguir ver a imagem.
    try:
        texto_pagina = await page.inner_text("body")
        print("Texto visível da página (primeiros 1000 caracteres):")
        print(texto_pagina[:1000])
    except Exception as erro_texto:
        print(f"Não foi possível extrair texto da página: {erro_texto}")

    print("--- FIM DO DIAGNÓSTICO ---")

    # Em vez de esperar "networkidle" (que pode nunca disparar em SPAs com
    # polling em segundo plano), esperamos o próximo marco real do fluxo:
    # o ícone do menu principal aparecer, o que confirma que o login
    # completou e a aplicação carregou.
    print("Aguardando aplicação carregar após login...")
    await localizar_em_frames(page, 'svg[data-icon="menu"]', timeout=TIMEOUT_LOGIN, descricao="ícone do menu (pós-login)")

# ===================================================

async def abrir_lista(page):
    """
    Navega, dentro do sistema já logado, até a tela que lista os itens
    a serem aprovados ("Minhas Tarefas"):
      1. Clica no ícone do menu principal.
      2. Abre o submenu "Início".
      3. Clica no item "Minhas Tarefas".
      4. Registra um diagnóstico (screenshot + lista de frames abertos)
         para facilitar a depuração caso algo dê errado nessa transição.
      5. Faz um "inventário" de checkboxes e botões relevantes em cada
         frame, útil para descobrir o seletor certo caso o sistema mude
         de layout no futuro.
      6. Espera o checkbox "Selecionar Todos" aparecer, o que confirma
         que a lista de tarefas realmente carregou.

    Parâmetros:
        page (Page): a página do Playwright já autenticada no sistema.

    Não retorna valor; ao final, a lista de tarefas estará visível e
    pronta para a próxima etapa (selecionar_todos()).
    """

    print("Abrindo lista...")

    # FIX: busca o ícone do menu tanto na página quanto em frames,
    # e espera ele ficar visível em vez de depender de networkidle.
    menu_icon = await localizar_em_frames(
        page, 'svg[data-icon="menu"]', descricao="ícone do menu"
    )
    await menu_icon.click(force=True)

    # pequena pausa para a animação de abertura do menu (Ant Design)
    await page.wait_for_timeout(500)

    submenu_inicio = await localizar_em_frames(
        page, 'div.ant-menu-submenu-title:has-text("Início")', descricao="submenu Início"
    )
    await submenu_inicio.click(force=True)

    await page.wait_for_timeout(500)

    item_minhas_tarefas = await localizar_em_frames(
        page, "#minhas-tarefas", descricao="item Minhas Tarefas"
    )
    await item_minhas_tarefas.click(force=True)

    # DIAGNÓSTICO: dá um tempo pra tela carregar e registra o estado atual
    # (screenshot + urls de todos os frames) ANTES de esperar pelo checkbox.
    # Isso serve pra descobrirmos exatamente o que aparece nessa tela.
    await page.wait_for_timeout(3000)

    print("--- DIAGNÓSTICO: estado da página após clicar em Minhas Tarefas ---")
    print(f"URL da página principal: {page.url}")
    print(f"Título da página: {await page.title()}")
    print(f"Frames abertos ({len(page.frames)}):")
    for i, frame in enumerate(page.frames):
        print(f"  [{i}] {frame.url}")

    try:
        await page.screenshot(path="debug_apos_minhas_tarefas.png", full_page=True)
        print("Screenshot salvo em: debug_apos_minhas_tarefas.png")
    except Exception as erro_shot:
        print(f"Não foi possível salvar screenshot de diagnóstico: {erro_shot}")
    print("--- FIM DO DIAGNÓSTICO ---")

    # DIAGNÓSTICO 2: inventaria checkboxes e botões relevantes em cada frame,
    # pra descobrirmos o ID/seletor real usado nessa tela (em vez de continuar
    # chutando o nome do elemento).
    print("--- INVENTÁRIO DE ELEMENTOS POR FRAME ---")

    # Este trecho de JavaScript é executado dentro de cada frame da página
    # (via frame.evaluate). Ele varre o HTML carregado naquele frame e
    # devolve, em formato JSON, uma lista de checkboxes (com id, name,
    # classe e se está visível) e uma lista de botões/links cujo texto
    # contenha palavras como "pesquisar", "buscar", "selecionar", "todos"
    # ou "filtrar". Serve como um raio-x rápido da tela para localizar
    # os seletores certos sem precisar inspecionar manualmente no navegador.
    js_inventario = """
    () => {
        const checkboxes = Array.from(document.querySelectorAll('input[type="checkbox"]')).map(el => ({
            id: el.id,
            name: el.name,
            classe: el.className,
            visivel: el.offsetParent !== null
        }));

        const botoes = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], a'))
            .filter(el => /pesquis|busca|selecionar|todos|filtr/i.test((el.textContent || el.value || '')))
            .map(el => ({
                tag: el.tagName,
                id: el.id,
                texto: (el.textContent || el.value || '').trim()
            }));

        return JSON.stringify({ checkboxes, botoes }, null, 2);
    }
    """

    for frame in page.frames:
        try:
            resultado = await frame.evaluate(js_inventario)
            print(f"\\nFrame: {frame.url}")
            print(resultado)
        except Exception as erro_inventario:
            print(f"\\nFrame: {frame.url} -> não foi possível inventariar ({erro_inventario})")

    print("--- FIM DO INVENTÁRIO ---")

    # Espera algo concreto da próxima tela em vez de networkidle
    try:
        await localizar_em_frames(
            page, "#ckbTodosMovelPesquisa",
            timeout=45000,
            descricao="checkbox Selecionar Todos (confirma que a lista abriu)"
        )
    except Exception:
        # Se não achar, salva mais um screenshot no momento exato da falha
        try:
            await page.screenshot(path="debug_falha_checkbox.png", full_page=True)
            print("Screenshot da falha salvo em: debug_falha_checkbox.png")
        except Exception:
            pass
        raise

    print("Lista aberta com sucesso.")

# ===================================================

async def selecionar_todos(page):
    """
    Marca o checkbox "Selecionar Todos" na tela de lista de tarefas,
    fazendo com que todos os itens pendentes de aprovação sejam
    selecionados de uma vez.

    Parâmetros:
        page (Page): a página do Playwright com a lista já aberta.

    Não retorna valor; apenas produz o efeito colateral de marcar o
    checkbox na tela.
    """

    checkbox = await localizar_em_frames(
        page, "#ckbTodosMovelPesquisa", descricao="checkbox Selecionar Todos"
    )

    await checkbox.click(force=True)

    print("Todos os registros foram selecionados.")

# ===================================================

async def aprovar(page):
    """
    Clica no botão "Aprovar" para os itens selecionados e identifica
    onde a próxima etapa (a tela de justificativa) vai acontecer —
    pois o clique pode abrir uma nova aba/popup, ou pode simplesmente
    navegar na mesma página, dependendo do comportamento do sistema.

    Estratégia usada:
      1. Guarda a lista de abas abertas antes do clique.
      2. Clica no botão "Aprovar" já esperando por um popup
         (page.expect_popup()). Se um popup realmente abrir, ele é
         retornado.
      3. Se nenhum popup for detectado dessa forma, o código espera um
         pouco e compara a lista de abas antes/depois do clique, para
         ver se uma nova aba apareceu por outro caminho.
      4. Se ainda assim nenhuma aba nova for encontrada, assume que a
         navegação ocorreu na própria página original, e retorna essa
         mesma página.

    Parâmetros:
        page (Page): a página do Playwright com os itens já selecionados.

    Retorna:
        A página (Page) onde a tela de justificativa deve aparecer —
        pode ser uma aba nova ou a mesma página original.
    """
    print("Buscando o botão Aprovar nas camadas da página...")

    botao_aprovar = await localizar_em_frames(
        page, "#Action_btnAprovar", descricao="botão Aprovar"
    )

    print("Botão Aprovar localizado.")

    # Guarda as páginas existentes antes do clique
    paginas_antes = list(page.context.pages)

    print(f"Quantidade de abas antes do clique: {len(paginas_antes)}")

    # Tenta capturar uma nova aba/popup
    try:

        async with page.expect_popup() as popup_info:
            await botao_aprovar.click(force=True)

        nova_aba = await popup_info.value

        print("Nova aba/popup detectado!")

        await nova_aba.wait_for_load_state(
            "domcontentloaded",
            timeout=15000
        )

        print(f"URL da nova aba: {nova_aba.url}")

        return nova_aba

    except Exception as erro_popup:

        print(f"Popup não detectado: {erro_popup}")

        # Aguarda um pouco para verificar se uma nova página foi criada
        await page.wait_for_timeout(3000)

        paginas_depois = list(page.context.pages)

        print(f"Quantidade de abas depois do clique: {len(paginas_depois)}")

        # Verifica se abriu uma nova aba
        novas_paginas = [
            pagina
            for pagina in paginas_depois
            if pagina not in paginas_antes
        ]

        if novas_paginas:

            nova_aba = novas_paginas[-1]

            print("Nova aba encontrada após o clique!")
            print(f"URL: {nova_aba.url}")

            await nova_aba.wait_for_load_state(
                "domcontentloaded",
                timeout=15000
            )

            return nova_aba

        # Caso não tenha aberto nova aba,
        # verifica se a própria página mudou
        print("Nenhuma nova aba encontrada.")

        await page.wait_for_timeout(2000)

        print(f"URL atual: {page.url}")

        return page

# ===================================================

async def justificar(nova_aba):
    """
    Preenche e salva a justificativa de aprovação na tela indicada
    (que pode ser uma aba nova ou a página original, dependendo do que
    a função aprovar() retornou):
      1. Espera a página terminar de carregar.
      2. Localiza o campo de texto da justificativa e digita o texto
         padrão definido em JUSTIFICATIVA.
      3. Localiza e clica no botão "Salvar".

    Parâmetros:
        nova_aba (Page): a página onde está o formulário de
            justificativa (retornada por aprovar()).

    Não retorna valor; ao final, a justificativa estará salva no sistema.
    """

    print("Acessando a tela da justificativa...")

    await nova_aba.wait_for_load_state(
        "domcontentloaded",
        timeout=15000
    )

    print(f"URL atual: {nova_aba.url}")

    campo_texto = await localizar_em_frames(
        nova_aba, "#ctl00_conteudoPagina_tbxDsJustificativa", descricao="campo de justificativa"
    )

    print("Preenchendo justificativa...")

    await campo_texto.click()
    await campo_texto.fill(JUSTIFICATIVA)

    print("Justificativa preenchida com sucesso!")

    botao_salvar = await localizar_em_frames(
        nova_aba, "#ctl00_conteudoBotoes_btnSalvar", descricao="botão Salvar"
    )

    print("Clicando em Salvar...")

    await botao_salvar.click(force=True)

    print("Botão Salvar clicado!")

    await nova_aba.wait_for_timeout(2000)

    print("Justificativa salva.")

# ===================================================

async def executar():
    """
    Função principal: orquestra todo o fluxo do robô, do início ao fim.

    Passo a passo:
      1. Inicializa o Playwright e abre um navegador Chromium usando um
         "perfil persistente" (uma pasta dedicada em disco que guarda
         cookies, sessão etc. entre execuções — como um perfil separado
         do Chrome, só para esta automação).
      2. Executa, em sequência, as etapas do fluxo de aprovação:
         login() -> abrir_lista() -> selecionar_todos() -> aprovar()
         -> justificar().
      3. Se tudo ocorrer bem, registra sucesso no log e na tela.
      4. Se qualquer etapa falhar, captura o erro, registra o traceback
         completo, grava no log e salva um screenshot com data/hora no
         nome do arquivo, para facilitar o diagnóstico posterior.
      5. Ao final (com sucesso ou falha), sempre fecha o navegador e
         encerra o processo Python.

    Não recebe parâmetros nem retorna valor — é a função chamada pelo
    bloco "if __name__ == '__main__':" no fim do arquivo.
    """

    print("=== VERSÃO DO SCRIPT: v6-aceitar-cookies ===")

    async with async_playwright() as p:

        # Perfil dedicado só pra automação (não é o seu perfil real do Chrome).
        # Evita qualquer extensão de segurança/DLP corporativa que esteja
        # instalada no seu perfil padrão e que possa estar interceptando a
        # janela quando detecta automação/sandbox desativado.
        PASTA_PERFIL_AUTOMACAO = r"C:\automacao_bernoulli\chrome_profile"
        os.makedirs(PASTA_PERFIL_AUTOMACAO, exist_ok=True)

        # TESTE DE DIAGNÓSTICO: headless=True (sem janela visível), só pra
        # confirmar se o bloqueio é específico da renderização da janela
        # visível nessa máquina. Se funcionar assim, o problema é isolado
        # na exibição da janela (GPU/segurança/virtualização), não no
        # Playwright nem no fluxo do script em si.
        context = await p.chromium.launch_persistent_context(
            user_data_dir=PASTA_PERFIL_AUTOMACAO,
            headless=True,
            viewport={"width": 1600, "height": 1000},
            args=[
                "--disable-infobars",
                "--test-type",
            ],
        )

        page = context.pages[0] if context.pages else await context.new_page()

        try:
            await login(page)
            await abrir_lista(page)
            await selecionar_todos(page)

            # 1. Aprova e recebe a referência da nova aba aberta
            aba_justificativa = await aprovar(page)

            # 2. Executa a escrita da justificativa na nova aba
            await justificar(aba_justificativa)

            print("Processo concluído.")
            logging.info("Sucesso.")

        except Exception as e:
            print(f"\nERRO: {e}\n")
            traceback.print_exc()
            logging.error(f"Falha na execução: {e}")

            # Screenshot de erro com timestamp para facilitar diagnóstico
            nome_screenshot = f"erro_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"

            try:
                await page.screenshot(
                    path=nome_screenshot,
                    full_page=True
                )
                print(f"Screenshot salvo em: {nome_screenshot}")
            except Exception as erro_screenshot:
                print(f"Não foi possível salvar o screenshot: {erro_screenshot}")

        finally:
            print("Fechando o contexto do navegador...")
            await context.close()

            print("Encerrando o script...")
            os._exit(0)

# ===================================================

# Ponto de entrada do script: só executa o fluxo completo (executar())
# quando o arquivo é rodado diretamente (ex.: "python robo_aprovador.py"),
# e não quando é apenas importado por outro módulo.
if __name__ == "__main__":
    asyncio.run(executar())
