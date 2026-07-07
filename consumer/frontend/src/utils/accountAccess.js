export const getVisibleAccountUsers = ({ mode = 'admin', currentUser = null, users = [] } = {}) => {
  const sourceUsers = Array.isArray(users) ? users : [];
  if (mode === 'admin') {
    return sourceUsers;
  }

  if (!currentUser?.username) {
    return [];
  }

  const currentUserFromList = sourceUsers.find((user) => user.username === currentUser.username);
  return [currentUserFromList || currentUser];
};

export const getAccountUserAction = ({ mode = 'admin', currentUser = null, targetUser = null } = {}) => {
  if (currentUser?.username && targetUser?.username === currentUser.username) {
    if (mode === 'admin') {
      return 'current_account';
    }
    return 'change_password';
  }
  return 'reset_password';
};
