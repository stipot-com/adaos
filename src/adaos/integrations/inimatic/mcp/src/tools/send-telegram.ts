import { httpJson } from '../http.js';

export async function inimaticSendTelegram(args: {
  text: string;
  hubId?: string;
  chatId?: string;
  botId?: string;
}) {
  if (!args.text) {
    throw new Error('text is required');
  }

  return httpJson('POST', '/io/tg/send', {
    body: {
      text: args.text,
      hub_id: args.hubId,
      chat_id: args.chatId,
      bot_id: args.botId
    }
  });
}
