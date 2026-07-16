const state = {
  selectedCharacterId: window.localStorage.getItem("kokoro.selectedCharacterId"),
  conversationId: null,
  providers: [],
  characters: [],
};

const elements = {
  characterList: document.getElementById("character-list"),
  providerSelect: document.getElementById("provider-select"),
  providerNote: document.getElementById("provider-note"),
  messageList: document.getElementById("message-list"),
  conversationMeta: document.getElementById("conversation-meta"),
  toast: document.getElementById("toast"),
  form: {
    name: document.getElementById("character-name"),
    summary: document.getElementById("character-summary"),
    personality: document.getElementById("character-personality"),
    interests: document.getElementById("character-interests"),
    speakingStyle: document.getElementById("character-speaking-style"),
    boundaries: document.getElementById("character-boundaries"),
    emotion: document.getElementById("state-emotion"),
    affection: document.getElementById("state-affection"),
    fatigue: document.getElementById("state-fatigue"),
    trust: document.getElementById("state-trust"),
    energy: document.getElementById("state-energy"),
  },
  actions: {
    refreshCharacters: document.getElementById("refresh-characters-button"),
    createCharacter: document.getElementById("create-character-button"),
    updateCharacter: document.getElementById("update-character-button"),
    resetConversation: document.getElementById("reset-conversation-button"),
    chatForm: document.getElementById("chat-form"),
    chatInput: document.getElementById("chat-input"),
  },
};

function csvToList(value) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function showToast(message, isError = false) {
  elements.toast.hidden = false;
  elements.toast.textContent = message;
  elements.toast.style.background = isError ? "rgba(140, 47, 47, 0.94)" : "rgba(45, 34, 29, 0.92)";
  window.clearTimeout(showToast.timerId);
  showToast.timerId = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, 2400);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    ...options,
  });

  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.detail || `Request failed: ${response.status}`);
  }

  if (response.status === 204) {
    return null;
  }
  return response.json();
}

function buildCharacterPayload() {
  return {
    name: elements.form.name.value.trim(),
    summary: elements.form.summary.value.trim(),
    personality: csvToList(elements.form.personality.value),
    interests: csvToList(elements.form.interests.value),
    speaking_style: elements.form.speakingStyle.value.trim() || "natural",
    boundaries: csvToList(elements.form.boundaries.value),
    initial_state: {
      emotion: elements.form.emotion.value.trim() || "neutral",
      affection: Number(elements.form.affection.value || 0),
      fatigue: Number(elements.form.fatigue.value || 0),
      trust: Number(elements.form.trust.value || 0),
      energy: Number(elements.form.energy.value || 100),
    },
  };
}

function buildCharacterPatchPayload() {
  return {
    name: elements.form.name.value.trim(),
    summary: elements.form.summary.value.trim(),
    personality: csvToList(elements.form.personality.value),
    interests: csvToList(elements.form.interests.value),
    speaking_style: elements.form.speakingStyle.value.trim() || "natural",
    boundaries: csvToList(elements.form.boundaries.value),
    state: {
      emotion: elements.form.emotion.value.trim() || "neutral",
      affection: Number(elements.form.affection.value || 0),
      fatigue: Number(elements.form.fatigue.value || 0),
      trust: Number(elements.form.trust.value || 0),
      energy: Number(elements.form.energy.value || 100),
    },
  };
}

function fillCharacterForm(character) {
  elements.form.name.value = character.name || "";
  elements.form.summary.value = character.summary || "";
  elements.form.personality.value = (character.personality || []).join(", ");
  elements.form.interests.value = (character.interests || []).join(", ");
  elements.form.speakingStyle.value = character.speaking_style || "natural";
  elements.form.boundaries.value = (character.boundaries || []).join(", ");
  elements.form.emotion.value = character.state?.emotion || "neutral";
  elements.form.affection.value = character.state?.affection ?? 0;
  elements.form.fatigue.value = character.state?.fatigue ?? 0;
  elements.form.trust.value = character.state?.trust ?? 0;
  elements.form.energy.value = character.state?.energy ?? 100;
}

function renderCharacters() {
  elements.characterList.innerHTML = "";

  if (state.characters.length === 0) {
    elements.characterList.innerHTML = "<p class='hint'>目前還沒有角色，先建立一個。</p>";
    return;
  }

  state.characters.forEach((character) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `character-card ${character.id === state.selectedCharacterId ? "active" : ""}`;
    button.innerHTML = `
      <strong>${character.name}</strong>
      <span>${character.summary || "尚未填寫摘要"}</span>
      <span class="hint">情緒 ${character.state.emotion} / 好感 ${character.state.affection} / 疲勞 ${character.state.fatigue}</span>
    `;
    button.addEventListener("click", () => {
      selectCharacter(character.id);
    });
    elements.characterList.appendChild(button);
  });
}

function renderProviders() {
  elements.providerSelect.innerHTML = "";
  state.providers.forEach((providerId) => {
    const option = document.createElement("option");
    option.value = providerId;
    option.textContent = providerId;
    elements.providerSelect.appendChild(option);
  });

  if (state.providers.includes("lmstudio")) {
    elements.providerSelect.value = "lmstudio";
    elements.providerNote.textContent = "已偵測到 lmstudio provider。若本機有開啟 LM Studio server，聊天會直接走本地模型。";
  }
}

function appendMessage(role, content) {
  const message = document.createElement("article");
  message.className = `message ${role}`;
  message.textContent = content;
  elements.messageList.appendChild(message);
  elements.messageList.scrollTop = elements.messageList.scrollHeight;
}

function resetConversation() {
  state.conversationId = null;
  elements.messageList.innerHTML = "";
  elements.conversationMeta.textContent = "尚未建立對話";
}

async function loadProviders() {
  state.providers = await api("/api/v1/system/providers");
  renderProviders();
}

async function loadCharacters() {
  state.characters = await api("/api/v1/characters");
  renderCharacters();

  if (state.selectedCharacterId) {
    const target = state.characters.find((item) => item.id === state.selectedCharacterId);
    if (target) {
      fillCharacterForm(target);
    }
  }
}

async function selectCharacter(characterId) {
  const character = await api(`/api/v1/characters/${characterId}`);
  state.selectedCharacterId = character.id;
  window.localStorage.setItem("kokoro.selectedCharacterId", character.id);
  fillCharacterForm(character);
  resetConversation();
  renderCharacters();
  showToast(`已切換到角色 ${character.name}`);
}

async function createCharacter() {
  const payload = buildCharacterPayload();
  if (!payload.name) {
    throw new Error("角色名稱不能為空");
  }

  const character = await api("/api/v1/characters", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  state.selectedCharacterId = character.id;
  window.localStorage.setItem("kokoro.selectedCharacterId", character.id);
  fillCharacterForm(character);
  await loadCharacters();
  showToast(`角色 ${character.name} 已建立`);
}

async function updateCharacter() {
  if (!state.selectedCharacterId) {
    throw new Error("請先建立或選擇角色");
  }

  const character = await api(`/api/v1/characters/${state.selectedCharacterId}`, {
    method: "PATCH",
    body: JSON.stringify(buildCharacterPatchPayload()),
  });
  fillCharacterForm(character);
  await loadCharacters();
  showToast(`角色 ${character.name} 已更新`);
}

async function sendMessage() {
  if (!state.selectedCharacterId) {
    throw new Error("請先建立或選擇角色");
  }

  const content = elements.actions.chatInput.value.trim();
  if (!content) {
    throw new Error("訊息不能為空");
  }

  appendMessage("user", content);
  elements.actions.chatInput.value = "";

  const response = await api("/api/v1/chat/messages", {
    method: "POST",
    body: JSON.stringify({
      character_id: state.selectedCharacterId,
      conversation_id: state.conversationId,
      provider_id: elements.providerSelect.value || "fake",
      message: content,
    }),
  });

  state.conversationId = response.conversation_id;
  elements.conversationMeta.textContent = `conversation_id: ${response.conversation_id}`;
  if (response.assistant_message) {
    appendMessage("assistant", response.assistant_message.content);
  }

  const activeCharacter = state.characters.find((item) => item.id === state.selectedCharacterId);
  if (activeCharacter) {
    activeCharacter.state = response.state;
    fillCharacterForm(activeCharacter);
    renderCharacters();
  }
}

async function boot() {
  try {
    await Promise.all([loadProviders(), loadCharacters()]);
  } catch (error) {
    showToast(error.message, true);
  }
}

elements.actions.refreshCharacters.addEventListener("click", async () => {
  try {
    await loadCharacters();
    showToast("角色列表已更新");
  } catch (error) {
    showToast(error.message, true);
  }
});

elements.actions.createCharacter.addEventListener("click", async () => {
  try {
    await createCharacter();
  } catch (error) {
    showToast(error.message, true);
  }
});

elements.actions.updateCharacter.addEventListener("click", async () => {
  try {
    await updateCharacter();
  } catch (error) {
    showToast(error.message, true);
  }
});

elements.actions.resetConversation.addEventListener("click", () => {
  resetConversation();
  showToast("已清空目前對話狀態");
});

elements.actions.chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await sendMessage();
  } catch (error) {
    showToast(error.message, true);
  }
});

boot();
