chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.url || !tab.url.includes("youtube.com/playlist")) {
    chrome.scripting.executeScript({target:{tabId:tab.id}, func:()=>alert("Open https://www.youtube.com/playlist?list=LL first, then click the extension.")});
    return;
  }
  chrome.scripting.executeScript({target:{tabId:tab.id}, files:["content.js"]});
});
