document.addEventListener('DOMContentLoaded', () => {
    // 从HTML中嵌入的JSON获取初始化数据
    const chatDataElement = document.getElementById('chat-data');
    const initialData = JSON.parse(chatDataElement.textContent);
    
    const chatWindow = document.getElementById('chat-window');
    const chatForm = document.getElementById('chat-form');
    const chatInput = document.getElementById('chat-input');
    const sendButton = document.getElementById('send-button');

    // 聊天历史记录，包含系统上下文（如果存在）
    let messages = [];
    if (initialData.system_context) {
        messages.push({ role: 'system', content: initialData.system_context, is_context: true });
    }
    messages = messages.concat(initialData.history);

    // 滚动到聊天窗口底部
    const scrollToBottom = () => {
        chatWindow.scrollTop = chatWindow.scrollHeight;
    };

    // 渲染一条消息到窗口
    const renderMessage = (role, text) => {
        const messageWrapper = document.createElement('div');
        messageWrapper.className = `flex items-start gap-3 ${role === 'user' ? 'justify-end' : ''}`;
        
        let messageHtml;
        if (role === 'user') {
            messageHtml = `
                <div class="bg-blue-500 text-white p-3 rounded-lg max-w-lg">
                    <p class="text-sm">${text}</p>
                </div>
            `;
        } else {
            messageHtml = `
                <div class="p-2 bg-gray-700 text-white rounded-full h-8 w-8 flex items-center justify-center font-bold text-sm">AI</div>
                <div class="assistant-bubble bg-gray-200 p-3 rounded-lg max-w-lg">
                    <p class="text-sm">${text}</p>
                </div>
            `;
        }
        messageWrapper.innerHTML = messageHtml;
        chatWindow.appendChild(messageWrapper);
        return messageWrapper;
    };

    // 保存聊天记录到后端
    const saveHistory = async () => {
        // 过滤掉系统上下文，只保存真实的对话
        const historyToSave = messages.filter(msg => !msg.is_context);
        try {
            await fetch('/chat/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ history: historyToSave })
            });
        } catch (error) {
            console.error('Failed to save chat history:', error);
        }
    };

    // 处理表单提交
    chatForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const userInput = chatInput.value.trim();
        if (!userInput) return;

        // 禁用输入框和按钮
        chatInput.value = '';
        chatInput.disabled = true;
        sendButton.disabled = true;

        // 将用户消息添加到历史并渲染
        messages.push({ role: 'user', content: userInput });
        renderMessage('user', userInput);
        scrollToBottom();

        // 创建一个用于流式响应的空消息气泡
        const assistantMessageWrapper = renderMessage('assistant', '<i class="fas fa-spinner fa-spin"></i>');
        const assistantTextElement = assistantMessageWrapper.querySelector('.assistant-bubble p');
        assistantTextElement.innerHTML = ''; // 清空初始内容
        scrollToBottom();
        
        try {
            const response = await fetch('/chat/stream', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ messages })
            });

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let assistantResponseText = '';

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                
                const chunk = decoder.decode(value);
                assistantResponseText += chunk;
                // 将 Markdown 换行符转换为 HTML <br>
                assistantTextElement.innerHTML = assistantResponseText.replace(/\n/g, '<br>');
                scrollToBottom();
            }

            // 对话结束后，将完整的AI回复添加到历史记录
            messages.push({ role: 'assistant', content: assistantResponseText });
            await saveHistory();

        } catch (error) {
            console.error('Error during chat stream:', error);
            assistantTextElement.innerHTML = '抱歉，AI服务当前不可用，请稍后再试。';
        } finally {
            // 重新启用输入框和按钮
            chatInput.disabled = false;
            sendButton.disabled = false;
            chatInput.focus();
        }
    });

    // 初始加载时滚动到底部
    scrollToBottom();
});
